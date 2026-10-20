"""Bounded Telegram-to-Telegram relay. No whole-file buffering on the fast path."""

import asyncio
import hashlib
import math
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pyrogram
from pyrogram import raw, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    AuthKeyDuplicated, FilePartMissing, FileReferenceExpired, FileReferenceInvalid,
    FloodWait, FloodPremiumWait, InternalServerError, ServiceUnavailable,
)

from config import PyroConf
from helpers.fast_download import CHUNK_SIZE, MTProtoWorkerSession, extract_media_info, get_file_location
from helpers.transfer_window import UploadWindow
from helpers.transfer_resources import ResourceMonitor
from logger import LOGGER

PART_SIZE = 512 * 1024
TRANSIENT = (OSError, TimeoutError, InternalServerError, ServiceUnavailable)
REFERENCES = (FileReferenceExpired, FileReferenceInvalid)
_crypto_workers = None


def configure_crypto():
    """Configure Pyrofork's shared native crypto executor before clients start."""
    global _crypto_workers
    if _crypto_workers != PyroConf.TRANSFER_CRYPTO_WORKERS:
        previous = pyrogram.crypto_executor
        pyrogram.crypto_executor = ThreadPoolExecutor(
            max_workers=PyroConf.TRANSFER_CRYPTO_WORKERS, thread_name_prefix="TransferCrypto")
        previous.shutdown(wait=True)
        _crypto_workers = PyroConf.TRANSFER_CRYPTO_WORKERS


async def supervised(coroutines):
    """Wait for success, or cancel and drain every sibling before raising."""
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def abortable(coro, event=None):
    if event is None:
        return await coro
    if event.is_set():
        coro.close()
        raise asyncio.CancelledError("Transfer aborted before starting")
    task = asyncio.create_task(coro)
    watcher = asyncio.create_task(event.wait())
    try:
        await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if event.is_set():
            raise asyncio.CancelledError("Transfer aborted")
        return await task
    finally:
        for pending in (task, watcher):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)


async def retry(operation):
    for attempt in range(3):
        try:
            return await operation()
        except TRANSIENT:
            if attempt == 2:
                raise
            await asyncio.sleep((0.5, 1.0)[attempt])


async def final_request(client, query):
    """Retry the same upload handle/random IDs; never restart the byte pipeline."""
    rpc = type(query).__name__
    async def operation():
        LOGGER(__name__).info("Telegram finalization: rpc=%s timeout=%ss", rpc, PyroConf.TRANSFER_FINALIZE_TIMEOUT)
        try:
            return await client.invoke(query, retries=0, timeout=PyroConf.TRANSFER_FINALIZE_TIMEOUT)
        except TRANSIENT as error:
            LOGGER(__name__).warning("Telegram finalization retry: rpc=%s reason=%s", rpc, type(error).__name__)
            raise
    try:
        return await retry(operation)
    except TimeoutError as error:
        raise TimeoutError(f"Telegram {rpc} timed out after 3 attempts "
                           f"({PyroConf.TRANSFER_FINALIZE_TIMEOUT:g}s per request)") from error


async def delete_progress(message, timeout=5):
    if message is not None:
        try:
            await asyncio.wait_for(message.delete(), timeout=timeout)
        except Exception as error:
            LOGGER(__name__).debug("Progress cleanup failed: %s", type(error).__name__)


async def finalization_notice(message):
    if message is not None:
        try:
            await asyncio.wait_for(message.edit(
                "📤 **Upload complete**\nWaiting for Telegram to finish sending the media…\n"
                "The uploaded data is retained while this request is retried.",
                parse_mode=ParseMode.MARKDOWN), timeout=3)
        except Exception as error:
            LOGGER(__name__).debug("Finalization status edit failed: %s", type(error).__name__)


async def startup_timeout(coro):
    # asyncio.wait_for on Python 3.10 can swallow a concurrent cancellation
    # when startup finishes at the same instant. Explicit supervision avoids it.
    task = asyncio.create_task(coro)
    try:
        done, _ = await asyncio.wait((task,), timeout=60)
        if not done:
            raise TimeoutError("MTProto session startup timed out")
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ByteBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.peak = 0
        self.condition = asyncio.Condition()
        self.admission = asyncio.Lock()

    async def acquire(self, amount):
        if amount > self.limit:
            raise ValueError("Chunk exceeds transfer buffer")
        # FIFO admission keeps later chunks from consuming the memory needed
        # by an earlier chunk that the ordered producer is waiting for.
        async with self.admission:
            async with self.condition:
                await self.condition.wait_for(lambda: self.used + amount <= self.limit)
                self.used += amount
                self.peak = max(self.peak, self.used)

    async def release(self, amount):
        async with self.condition:
            self.used -= amount
            self.condition.notify_all()


class Credit:
    """Charge both downloaded bytes and their upload slices until acknowledged."""
    def __init__(self, budget, amount, parts):
        self.budget, self.amount, self.parts = budget, amount, parts

    async def acknowledged(self):
        self.parts -= 1
        if self.parts == 0:
            await self.release()

    async def release(self):
        if self.amount:
            await self.budget.release(self.amount)
            self.amount = 0


class ConnectionPool:
    """Fixed connections with bounded concurrent RPCs on each media session."""
    def __init__(self, count, requests=1):
        self.available = asyncio.Queue()
        self.capacity = count * requests
        self.requests = requests
        self.sessions = [None] * count
        self.keys = [None] * count
        self.users = [0] * count
        self.invalid = [False] * count
        self.conditions = [asyncio.Condition() for _ in range(count)]
        self.authorizations = {}
        self.auth_lock = asyncio.Lock()
        self.native_lock = asyncio.Lock()
        for _ in range(requests):
            for index in range(count):
                self.available.put_nowait(index)

    @asynccontextmanager
    async def lease(self, client, dc):
        index = await self.available.get()
        acquired = False
        condition = self.conditions[index]
        try:
            async with condition:
                # A DC switch or broken connection waits for all outstanding RPCs.
                await condition.wait_for(lambda: self.users[index] == 0 or (
                    self.keys[index] == (client, dc) and not self.invalid[index]))
                if self.keys[index] != (client, dc) or self.invalid[index]:
                    if self.sessions[index] is not None:
                        await self.sessions[index].stop()
                    self.sessions[index] = self.keys[index] = None
                    worker = MTProtoWorkerSession(client, dc)
                    worker.authorization_cache = self.authorizations
                    try:
                        if dc != await client.storage.dc_id():
                            async with self.auth_lock:
                                await startup_timeout(worker.start())
                        else:
                            await startup_timeout(worker.start())
                    except BaseException:
                        await worker.stop()
                        raise
                    self.sessions[index] = worker
                    self.keys[index] = (client, dc)
                    self.invalid[index] = False
                self.users[index] += 1
                acquired = True
            yield self.sessions[index]
        except (OSError, TimeoutError, AuthKeyDuplicated, asyncio.CancelledError):
            if acquired:
                self.invalid[index] = True
            raise
        finally:
            async with condition:
                if acquired:
                    self.users[index] -= 1
                    if self.users[index] == 0 and self.invalid[index]:
                        await self.sessions[index].stop()
                        self.sessions[index] = self.keys[index] = None
                condition.notify_all()
            self.available.put_nowait(index)

    async def close(self):
        await supervised([worker.stop() for worker in self.sessions if worker is not None])
        self.sessions[:] = [None] * len(self.sessions)
        self.keys[:] = [None] * len(self.keys)
        self.authorizations.clear()

    @asynccontextmanager
    async def native(self):
        # Drain permits atomically, then reserve two whole connections for
        # origin/CDN. Remaining connections must keep flowing: another file
        # may have reserved the bytes this native producer is waiting for.
        if len(self.sessions) < 2:
            raise ValueError("CDN streaming requires at least two download connection slots")
        async with self.native_lock:
            indices = []
            try:
                for _ in range(self.capacity):
                    index = await self.available.get()
                    indices.append(index)
                reserved = set(range(2))
                for index in reserved:
                    if self.sessions[index] is not None:
                        await self.sessions[index].stop()
                    self.sessions[index] = self.keys[index] = None
                returned = [index for index in indices if index not in reserved]
                indices = [index for index in indices if index in reserved]
                for index in returned:
                    self.available.put_nowait(index)
                yield
            finally:
                for index in indices:
                    self.available.put_nowait(index)


class CdnRedirect(Exception):
    pass


@dataclass
class Upload:
    source: object
    message: object
    kind: str
    size: int
    name: str
    mime: str
    file: object
    thumbnail: object = None


class TransferProgress:
    """One progress message, aggregated across an album."""
    def __init__(self, message=None):
        self.message = message
        self.started = None
        self.last_edit = self.last_log = time.monotonic()
        self.rate_at = self.last_edit
        self.rate_bytes = (0, 0)
        self.states = {}
        self.lock = asyncio.Lock()

    def begin(self):
        if self.started is None:
            self.started = self.last_edit = self.last_log = self.rate_at = time.monotonic()

    async def __call__(self, key, downloaded, uploaded, total):
        self.begin()
        self.states[key] = (downloaded, uploaded, total)
        now = time.monotonic()
        down, up, size = map(sum, zip(*self.states.values()))
        elapsed = max(now - self.started, 0.001)
        if now - self.last_log >= 15:
            self.last_log = now
            LOGGER(__name__).info(
                "Relay progress: downloaded=%s acknowledged=%s total=%s elapsed=%.1fs "
                "download=%.2f MiB/s acknowledged-upload=%.2f MiB/s",
                down, up, size, elapsed, down / elapsed / CHUNK_SIZE, up / elapsed / CHUNK_SIZE)
        if self.message is None:
            return
        # A slow message edit must not hold all data workers behind its lock.
        if self.lock.locked() or now - self.last_edit < 5:
            return
        async with self.lock:
            self.last_edit = now
            interval = max(now - self.rate_at, 0.001)
            down_rate = max(0, down - self.rate_bytes[0]) / interval / CHUNK_SIZE
            up_rate = max(0, up - self.rate_bytes[1]) / interval / CHUNK_SIZE
            self.rate_at, self.rate_bytes = now, (down, up)
            fraction = min(up / size, 1)
            filled = int(fraction * 12)
            bar = "▰" * filled + "▱" * (12 - filled)
            text = (
                f"⚡ **TRANSFER LIVE**\n{bar}  `{int(fraction * 100)}%`\n\n"
                f"📥 **Download**\n"
                f"Downloaded: {down / CHUNK_SIZE:.1f}/{size / CHUNK_SIZE:.1f} MiB\n"
                f"Current speed: `{down_rate:.2f} MiB/s`\n"
                f"Average speed: `{down / elapsed / CHUNK_SIZE:.2f} MiB/s`\n\n"
                f"📤 **Upload • acknowledged by Telegram**\n"
                f"Uploaded (confirmed): {up / CHUNK_SIZE:.1f}/{size / CHUNK_SIZE:.1f} MiB\n"
                f"Current speed: `{up_rate:.2f} MiB/s`\n"
                f"Average speed: `{up / elapsed / CHUNK_SIZE:.2f} MiB/s`\n\n"
                f"⏱ **Elapsed** `{elapsed:.0f}s`  •  🧠 **Streaming in memory**"
            )
            try:
                await asyncio.wait_for(self.message.edit(text, parse_mode=ParseMode.MARKDOWN), timeout=3)
            except (FloodWait, AuthKeyDuplicated, pyrogram.StopTransmission):
                raise
            except Exception as error:
                LOGGER(__name__).debug("Progress edit failed: %s", error)


class TransferManager:
    def __init__(self, downloads=None, uploads=None, active=None, buffer_mib=None,
                 download_requests=None, upload_requests=None, adaptive=None):
        # Explicit request counts select a fixed window for controlled benchmarks.
        self.adaptive = (PyroConf.TRANSFER_ADAPTIVE and upload_requests is None
                         if adaptive is None else adaptive)
        # Explicit source request counts retain the multiplexed path for A/B
        # benchmarks. Production defaults isolate stalled reads from siblings;
        # old environment request counts cannot silently re-enable multiplexing.
        self.isolated_reads = PyroConf.DOWNLOAD_ISOLATED_READS and download_requests is None
        if self.isolated_reads:
            download_requests = 1
        self.upload_initial = (uploads or PyroConf.PARALLEL_UPLOAD_WORKERS) * (
            upload_requests or PyroConf.UPLOAD_REQUESTS_PER_CONNECTION)
        self.download_initial = (downloads or PyroConf.PARALLEL_DOWNLOAD_WORKERS) * (
            download_requests or PyroConf.DOWNLOAD_REQUESTS_PER_CONNECTION)
        self.downloads = ConnectionPool(downloads or PyroConf.PARALLEL_DOWNLOAD_WORKERS,
            max(download_requests or PyroConf.DOWNLOAD_REQUESTS_PER_CONNECTION,
                PyroConf.MAX_DOWNLOAD_REQUESTS_PER_CONNECTION) if self.adaptive and not self.isolated_reads else
            download_requests or PyroConf.DOWNLOAD_REQUESTS_PER_CONNECTION)
        self.uploads = ConnectionPool(uploads or PyroConf.PARALLEL_UPLOAD_WORKERS,
            max(upload_requests or PyroConf.UPLOAD_REQUESTS_PER_CONNECTION,
                PyroConf.MAX_UPLOAD_REQUESTS_PER_CONNECTION) if self.adaptive else
            upload_requests or PyroConf.UPLOAD_REQUESTS_PER_CONNECTION)
        self.active_limit = active or PyroConf.MAX_ACTIVE_TRANSFERS
        if len(self.downloads.sessions) <= 2:
            # Native CDN uses both slots. Another producer holding memory and
            # waiting for those slots would prevent the CDN producer advancing.
            self.active_limit = 1
        self.active = asyncio.Semaphore(self.active_limit)
        self.budget = ByteBudget((buffer_mib or PyroConf.TRANSFER_BUFFER_MIB) * 1048576)
        self.disk_lock = asyncio.Lock()
        self.disk_reserved = 0
        self.paused_until = {"download": 0, "upload": 0}
        self.throttle_generation = [0]
        self.upload_users = set()
        self.profiles = {}
        self.download_profiles = {}
        self.profile_routes = {}
        self.rpc_count = {"download": 0, "upload": 0}
        self.rpc_seconds = {"download": 0.0, "upload": 0.0}
        self.timeout_count = {"download": 0, "upload": 0}
        self.resources = ResourceMonitor(PyroConf.TRANSFER_CPU_TARGET, PyroConf.TRANSFER_MEMORY_TARGET)
        self.upload_window = UploadWindow(self.upload_initial, self.uploads.capacity,
            len(self.uploads.sessions), PyroConf.TRANSFER_TUNE_INTERVAL,
            PyroConf.TRANSFER_TUNE_COOLDOWN, PyroConf.TRANSFER_TUNE_MIN_GAIN,
            self.resources.has_headroom, self.adaptive, self.resources.memory_pressure)
        self.download_window = UploadWindow(self.download_initial, self.downloads.capacity,
            len(self.downloads.sessions), PyroConf.TRANSFER_TUNE_INTERVAL,
            PyroConf.TRANSFER_TUNE_COOLDOWN, PyroConf.TRANSFER_TUNE_MIN_GAIN,
            self.resources.has_headroom, self.adaptive, self.resources.memory_pressure, latency_guard=True)

    async def invoke(self, worker, query, direction):
        """Expose server waits so all files pause dispatch in that direction."""
        attempts = 0
        while True:
            while self.paused_until[direction] > time.monotonic():
                await asyncio.sleep(self.paused_until[direction] - time.monotonic())
            try:
                window = self.upload_window if direction == "upload" else self.download_window
                # Refused requests re-enter the affected direction's smaller
                # window after waiting, instead of resuming as one burst.
                async with window.lease():
                    if self.paused_until[direction] > time.monotonic():
                        continue
                    requested = time.monotonic()
                    call = worker.session.invoke(query, sleep_threshold=0, retries=0,
                                                 timeout=PyroConf.TRANSFER_RPC_TIMEOUT)
                    if direction == "download":
                        # Session.invoke's timeout covers response waiting only,
                        # excluding readiness, encryption and socket drain. Bound
                        # the whole source RPC and drain cancellation before the
                        # pool retires the session (including its pending results).
                        result = await asyncio.wait_for(call, timeout=PyroConf.DOWNLOAD_STALL_TIMEOUT)
                    else:
                        result = await call
                    self.rpc_count[direction] += 1
                    self.rpc_seconds[direction] += time.monotonic() - requested
                    amount = 0
                    if result is True and isinstance(query, (
                            raw.functions.upload.SaveFilePart, raw.functions.upload.SaveBigFilePart)):
                        amount = len(query.bytes)
                    elif isinstance(result, raw.types.upload.File):
                        amount = len(result.bytes)
                    change = window.acknowledge(amount, latency=time.monotonic() - requested) if amount else None
                    if change:
                        if change == "source-latency":
                            self.download_profiles.clear()
                        if change == "memory-pressure":
                            self.profiles.clear()
                            self.download_profiles.clear()
                        identity = getattr(query, "file_id", getattr(getattr(query, "location", None), "id", 0))
                        LOGGER(__name__).info(
                            "transfer=%08x direction=%s tuning=%s window=%s confirmed=%s maximum=%s "
                            "sample=%.2f MiB/s", identity & 0xffffffff, direction, change,
                            window.limit, window.confirmed, window.maximum, window.rate / CHUNK_SIZE)
                    return result
            except TimeoutError:
                self.timeout_count[direction] += 1
                if direction == "download":
                    self.download_profiles.clear()
                    if self.download_window.stall():
                        LOGGER(__name__).warning("Source congestion: reduced read window to %s/%s",
                            self.download_window.limit, self.download_window.maximum)
                LOGGER(__name__).warning(
                    "Relay request timeout: direction=%s rpc=%s offset=%s part=%s timeout=%ss watchdog=%ss",
                    direction, type(query).__name__, getattr(query, "offset", None),
                    getattr(query, "file_part", None), PyroConf.TRANSFER_RPC_TIMEOUT,
                    PyroConf.DOWNLOAD_STALL_TIMEOUT if direction == "download" else None)
                raise
            except (FloodWait, FloodPremiumWait) as error:
                attempts += 1
                now = time.monotonic()
                already_paused = self.paused_until[direction] > now
                if not already_paused:
                    self.throttle_generation[0] += 1
                self.paused_until[direction] = max(
                    self.paused_until[direction], now + error.value)
                self.profiles.clear()
                self.download_profiles.clear()
                if not already_paused:
                    window = self.upload_window if direction == "upload" else self.download_window
                    if now >= window.recovery_until:
                        window.backoff()
                LOGGER(__name__).warning("Relay throttle: direction=%s reason=%s wait=%ss",
                                         direction, type(error).__name__, error.value)
                if error.value > 30 or attempts == 3:
                    raise
                # Retain the same part/file ID and payload during the wait.

    async def close(self):
        await supervised([self.downloads.close(), self.uploads.close()])

    def check_size(self, bot, size):
        limit = (4000 if getattr(getattr(bot, "me", None), "is_premium", False) else 2000) * 1048576
        if not size or size < 0:
            raise ValueError("Source media has no valid file size")
        if size > limit:
            raise ValueError(f"File exceeds the destination's {limit // 1048576} MiB upload limit")

    async def fetch(self, source, fid, offset, expected):
        async def operation():
            async with self.downloads.lease(source, fid.dc_id) as worker:
                response = await self.invoke(worker, raw.functions.upload.GetFile(
                    location=get_file_location(fid), offset=offset, limit=CHUNK_SIZE
                ), "download")
            if isinstance(response, raw.types.upload.FileCdnRedirect):
                raise CdnRedirect()
            if not isinstance(response, raw.types.upload.File) or len(response.bytes) != expected:
                raise ValueError(f"Invalid download chunk at {offset}: expected {expected} bytes")
            return response.bytes
        self.download_window.pending += 1
        try:
            for attempt in range(PyroConf.DOWNLOAD_CHUNK_ATTEMPTS):
                try:
                    return await operation()
                except TRANSIENT as error:
                    if attempt + 1 == PyroConf.DOWNLOAD_CHUNK_ATTEMPTS:
                        if isinstance(error, TimeoutError):
                            raise TimeoutError(f"Download chunk at byte {offset} stalled after "
                                f"{attempt + 1} attempts (source watchdog "
                                f"{PyroConf.DOWNLOAD_STALL_TIMEOUT:g}s; RPC timeout "
                                f"{PyroConf.TRANSFER_RPC_TIMEOUT:g}s)") from error
                        raise
                    LOGGER(__name__).warning("Retrying missing source chunk: offset=%s attempt=%s/%s reason=%s",
                        offset, attempt + 2, PyroConf.DOWNLOAD_CHUNK_ATTEMPTS, type(error).__name__)
                    await asyncio.sleep(min(0.25 * (attempt + 1), 1))
        finally:
            self.download_window.pending -= 1

    async def put_part(self, bot, file_id, part, count, size, data):
        dc = await bot.storage.dc_id()
        query = (raw.functions.upload.SaveBigFilePart(
            file_id=file_id, file_part=part, file_total_parts=count, bytes=data
        ) if size > 10 * 1048576 else raw.functions.upload.SaveFilePart(
            file_id=file_id, file_part=part, bytes=data
        ))
        async def operation():
            async with self.uploads.lease(bot, dc) as worker:
                result = await self.invoke(worker, query, "upload")
            if result is not True:
                raise OSError(f"Telegram did not acknowledge upload part {part}")
        self.upload_window.pending += 1
        try:
            await retry(operation)
        except TimeoutError as error:
            raise TimeoutError(f"Upload part {part} timed out after 3 attempts "
                               f"({PyroConf.TRANSFER_RPC_TIMEOUT:g}s per request)") from error
        finally:
            self.upload_window.pending -= 1

    async def prepare(self, source, bot, message, progress=None, abort_event=None):
        return await abortable(self._prepare(source, bot, message, progress), abort_event)

    async def _prepare(self, source, bot, message, progress):
        kind, media, encoded, size, name, mime = extract_media_info(message)
        self.check_size(bot, size)
        extensions = {"photo": ".jpg", "video": ".mp4", "animation": ".mp4", "video_note": ".mp4",
                      "audio": ".mp3", "voice": ".ogg"}
        name = safe_name(name, f"{kind}_{message.id}{extensions.get(kind, '.bin')}")
        async with self.active:
            if isinstance(progress, TransferProgress):
                progress.begin()
            for attempt in range(2):
                try:
                    try:
                        fid = pyrogram.file_id.FileId.decode(encoded)
                    except Exception:
                        return await self.disk_fallback(source, bot, message, progress)
                    try:
                        file = await self.pipe(source, bot, message, fid, size, name, progress)
                    except CdnRedirect:
                        # pipe() drains every task/credit before native streaming begins.
                        LOGGER(__name__).info("CDN redirect: using bounded native streaming for %s", name)
                        file = await self.pipe(source, bot, message, fid, size, name, progress, native=True)
                    return Upload(source, message, kind, size, name, mime, file)
                except REFERENCES:
                    if attempt:
                        raise
                    LOGGER(__name__).info("Refreshing expired media reference for message %s", message.id)
                    message = await refreshed_source(source, message)
                    new_kind, _, encoded, new_size, _, _ = extract_media_info(message)
                    if new_size != size or new_kind != kind:
                        raise ValueError("Source media changed during transfer")

    async def pipe(self, source, bot, message, fid, size, name, progress, native=False):
        started = time.monotonic()
        down_finished = up_finished = None
        queue = asyncio.Queue(self.uploads.capacity * 2)
        credits = set()
        pending = []
        file_id = bot.rnd_id()
        count = math.ceil(size / PART_SIZE)
        digest = hashlib.md5()
        window = self.upload_window
        transfer_id = f"{file_id & 0xffffffff:08x}"
        last_log = started
        token = object()
        profile_key = (id(bot), getattr(fid, "dc_id", None), native,
                       self.download_initial, self.downloads.capacity, self.upload_initial, self.uploads.capacity,
                       self.budget.limit)
        profile = self.profiles.get(profile_key)
        download_profile = self.download_profiles.get(profile_key)
        if not self.upload_users:
            self.resources.has_headroom()
            warm = profile[0] if (profile and profile[1] > started and self.adaptive
                                 and not self.resources.memory_pressure()) else None
            download_warm = download_profile[0] if (
                download_profile and download_profile[1] > started and self.adaptive
                and not self.resources.memory_pressure()) else None
            window.reset(warm)
            self.download_window.reset(download_warm)
            LOGGER(__name__).info(
                "transfer=%s start window=%s/%s download-window=%s/%s warm=%s/%s "
                "cpu=%.1f%% memory=%.1f%% cores=%.2f payload-buffer=%s MiB isolated-reads=%s",
                transfer_id, window.limit, window.maximum, self.download_window.limit,
                self.download_window.maximum, warm is not None, download_warm is not None,
                self.resources.cpu, self.resources.memory, self.resources.cores,
                self.budget.limit // CHUNK_SIZE, self.isolated_reads)
        self.upload_users.add(token)
        self.profile_routes[token] = profile_key
        completed = False
        generation = self.throttle_generation[0]
        downloaded = uploaded = 0
        last_bytes = (0, 0)
        last_rpc_count = self.rpc_count.copy()
        last_rpc_seconds = self.rpc_seconds.copy()
        key = (getattr(getattr(message, "chat", None), "id", None), message.id)

        async def notify():
            nonlocal last_log, last_bytes, last_rpc_count, last_rpc_seconds
            now = time.monotonic()
            if now - last_log >= 15:
                interval = now - last_log
                recent_down = (downloaded - last_bytes[0]) / interval / CHUNK_SIZE
                recent_up = (uploaded - last_bytes[1]) / interval / CHUNK_SIZE
                last_log = now
                last_bytes = (downloaded, uploaded)
                self.resources.has_headroom()
                latency = {direction: 1000 * (self.rpc_seconds[direction] - last_rpc_seconds[direction]) /
                    max(1, self.rpc_count[direction] - last_rpc_count[direction])
                    for direction in ("download", "upload")}
                last_rpc_count, last_rpc_seconds = self.rpc_count.copy(), self.rpc_seconds.copy()
                LOGGER(__name__).info(
                    "transfer=%s produced=%s acked=%s queue-estimate=%s window=%s/%s inflight=%s "
                    "buffer=%s/%s waiting=%s cpu=%.1f%% memory=%.1f%% "
                    "download-window=%s/%s download-inflight=%s recent-MiB/s=%.2f/%.2f "
                    "rpc-ms=%.1f/%.1f cpu-throttled=%.1f%% rss-MiB=%.1f timeouts=%s/%s",
                    transfer_id, downloaded, uploaded, queue.qsize() * PART_SIZE,
                    window.limit, window.maximum, window.active, self.budget.used,
                    self.budget.limit, "sink" if window.pending > window.active else "source",
                    self.resources.cpu, self.resources.memory, self.download_window.limit,
                    self.download_window.maximum, self.download_window.active, recent_down, recent_up,
                    latency["download"], latency["upload"], self.resources.throttled,
                    self.resources.process.memory_info().rss / CHUNK_SIZE,
                    self.timeout_count["download"], self.timeout_count["upload"])
            if progress:
                await progress(key, downloaded, uploaded, size)

        async def chunk(offset):
            nonlocal downloaded, down_finished
            expected = min(CHUNK_SIZE, size - offset)
            await self.budget.acquire(2 * expected)
            credit = Credit(self.budget, 2 * expected, math.ceil(expected / PART_SIZE))
            credits.add(credit)
            data = await self.fetch(source, fid, offset, expected)
            downloaded += len(data)
            if downloaded == size:
                down_finished = time.monotonic()
            await notify()
            return offset, data, credit

        async def enqueue(offset, data, credit):
            if size <= 10 * 1048576:
                digest.update(data)  # Producer consumes chunks in source order.
            for start in range(0, len(data), PART_SIZE):
                await queue.put(((offset + start) // PART_SIZE, data[start:start + PART_SIZE], credit))

        async def produce():
            nonlocal downloaded, down_finished
            if native:
                offset = 0
                # Reserve before requesting bytes; native GetFile validates CDN hashes.
                stream = source.stream_media(fid.encode()).__aiter__()
                try:
                    while offset < size:
                        expected = min(CHUNK_SIZE, size - offset)
                        await self.budget.acquire(2 * expected)
                        credit = Credit(self.budget, 2 * expected, math.ceil(expected / PART_SIZE))
                        credits.add(credit)
                        try:
                            data = await stream.__anext__()
                        except StopAsyncIteration:
                            raise ValueError("Native download ended before the advertised file size")
                        if len(data) != expected:
                            raise ValueError("Native download returned a truncated/oversized chunk")
                        downloaded += len(data)
                        if downloaded == size:
                            down_finished = time.monotonic()
                        await notify()
                        await enqueue(offset, data, credit)
                        offset += len(data)
                        data = None
                    # Do not retain the final data buffer while waiting for acknowledgements.
                    data = None
                finally:
                    await stream.aclose()
            else:
                offsets = iter(range(0, size, CHUNK_SIZE))
                try:
                    for _ in range(self.download_window.limit):
                        offset = next(offsets, None)
                        if offset is not None:
                            pending.append(asyncio.create_task(chunk(offset)))
                    while pending:
                        if size > 10 * CHUNK_SIZE:
                            # Big-file parts carry offsets and need no ordered MD5.
                            # Keep dispatching ready ranges when one source RPC stalls.
                            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                            task = next(task for task in pending if task in done)
                        else:
                            # Small-file MD5 must see bytes in original source order.
                            task = pending[0]
                        pending.remove(task)
                        try:
                            offset, data, credit = await task
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        await enqueue(offset, data, credit)
                        data = None
                        task = None
                        # Grow producers with the measured source window instead
                        # of reserving the maximum read-ahead before the first ACK.
                        while len(pending) < self.download_window.limit:
                            following = next(offsets, None)
                            if following is None:
                                break
                            pending.append(asyncio.create_task(chunk(following)))
                finally:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            for _ in range(self.uploads.capacity + 1):
                await queue.put(None)

        async def consume():
            nonlocal uploaded, up_finished
            while True:
                item = await queue.get()
                if item is None:
                    return
                part, data, credit = item
                await self.put_part(bot, file_id, part, count, size, data)
                uploaded += len(data)
                if uploaded == size:
                    up_finished = time.monotonic()
                await credit.acknowledged()
                if not credit.amount:
                    credits.discard(credit)
                data = item = None
                await notify()

        try:
            if native:
                async with self.downloads.native():
                    await supervised([produce(), *(consume() for _ in range(self.uploads.capacity + 1))])
            else:
                await supervised([produce(), *(consume() for _ in range(self.uploads.capacity + 1))])
            if downloaded != size or uploaded != size:
                raise ValueError("Transfer completed with missing bytes")
            completed = True
            if (generation == self.throttle_generation[0]
                    and set(self.profile_routes.values()) == {profile_key}
                    and not self.resources.memory_pressure()):
                for control, initial, profiles in (
                        (window, self.upload_initial, self.profiles),
                        (self.download_window, self.download_initial, self.download_profiles)):
                    if control.confirmed_rate > 0 and (control is self.download_window or control.confirmed >= initial):
                        if len(profiles) >= 32:
                            profiles.pop(next(iter(profiles)))
                        profiles[profile_key] = (control.confirmed, time.monotonic() + PyroConf.TRANSFER_WARM_TTL)
            elapsed = max(time.monotonic() - started, 0.001)
            down_seconds = max(down_finished - started, 0.001)
            up_seconds = max(up_finished - started, 0.001)
            LOGGER(__name__).info(
                "transfer=%s Relay complete: %s bytes, %.3fs, relay=%.2f MiB/s, download=%.2f MiB/s, "
                "acknowledged-upload=%.2f MiB/s, DC=%s, connections=%s/%s, requests=%s/%s, "
                "native=%s, buffer peak %s bytes",
                transfer_id, size, elapsed, size / elapsed / CHUNK_SIZE, size / down_seconds / CHUNK_SIZE,
                size / up_seconds / CHUNK_SIZE, getattr(fid, "dc_id", None),
                len(self.downloads.sessions), len(self.uploads.sessions), self.downloads.capacity,
                self.uploads.capacity, native, self.budget.peak)
            if size > 10 * 1048576:
                return raw.types.InputFileBig(id=file_id, parts=count, name=name)
            return raw.types.InputFile(id=file_id, parts=count, name=name, md5_checksum=digest.hexdigest())
        finally:
            if not completed:
                self.profiles.pop(profile_key, None)
                self.download_profiles.pop(profile_key, None)
            self.profile_routes.pop(token, None)
            self.upload_users.discard(token)
            for credit in credits:
                await credit.release()
            while not queue.empty():
                queue.get_nowait()

    async def repair(self, bot, upload, part):
        async with self.active:
            return await self._repair(bot, upload, part)

    async def _repair(self, bot, upload, part):
        if not 0 <= part < upload.file.parts:
            raise ValueError("Telegram requested an invalid missing upload part")
        offset = (part * PART_SIZE // CHUNK_SIZE) * CHUNK_SIZE
        expected = min(CHUNK_SIZE, upload.size - offset)
        await self.budget.acquire(2 * expected)
        try:
            for attempt in range(2):
                try:
                    encoded = extract_media_info(upload.message)[2]
                    try:
                        data = await self.fetch(upload.source, pyrogram.file_id.FileId.decode(encoded), offset, expected)
                    except CdnRedirect:
                        async with self.downloads.native():
                            stream = upload.source.stream_media(encoded, offset=offset // CHUNK_SIZE, limit=1)
                            try:
                                data = await stream.__anext__()
                            finally:
                                await stream.aclose()
                        if len(data) != expected:
                            raise ValueError("CDN repair returned a truncated chunk")
                    break
                except REFERENCES:
                    if attempt:
                        raise
                    upload.message = await refreshed_source(upload.source, upload.message)
                    if extract_media_info(upload.message)[3] != upload.size:
                        raise ValueError("Source media changed during upload repair")
            start = part * PART_SIZE - offset
            await self.put_part(bot, upload.file.id, part, upload.file.parts, upload.size,
                                data[start:start + PART_SIZE])
        finally:
            await self.budget.release(2 * expected)

    async def disk_fallback(self, source, bot, message, progress):
        """Unsupported location only: reserve disk, validate, upload, remove our files."""
        kind, _, _, size, name, mime = extract_media_info(message)
        reserve = PyroConf.DISK_RESERVE_MIB * 1048576
        directory = tempfile.gettempdir()
        async with self.disk_lock:
            if size + reserve > shutil.disk_usage(directory).free - self.disk_reserved:
                raise ValueError("Insufficient disk space for fallback; 256 MiB reserve must remain free")
            self.disk_reserved += size
        try:
            with tempfile.TemporaryDirectory(prefix="restrdl-", dir=directory) as folder:
                path = os.path.join(folder, safe_name(name, f"{kind}_{message.id}.bin"))
                async with self.downloads.native():
                    result = await message.download(file_name=path)
                if not result or os.path.getsize(result) != size:
                    raise ValueError("Native fallback did not save the complete file")
                # Upload through our acknowledged, bounded engine, not native save_file.
                async def fetch_local(_source, _fid, offset, expected):
                    def read():
                        with open(result, "rb") as handle:
                            handle.seek(offset)
                            return handle.read(expected)
                    data = await asyncio.to_thread(read)
                    if len(data) != expected:
                        raise ValueError("Fallback file changed during upload")
                    return data
                # Use an isolated producer adapter; shared pools and budgets remain global.
                adapter = LocalTransfer(self, fetch_local)
                file = await adapter.pipe(source, bot, message, None, size, os.path.basename(path), progress)
                return Upload(source, message, kind, size, os.path.basename(path), mime, file)
        finally:
            async with self.disk_lock:
                self.disk_reserved -= size


class LocalTransfer(TransferManager):
    def __init__(self, manager, fetch):
        self.__dict__ = manager.__dict__.copy()
        self.fetch = fetch


_manager = None
_manager_loop = None


def get_manager():
    global _manager, _manager_loop
    loop = asyncio.get_running_loop()
    if _manager is None or _manager_loop is not loop:
        _manager, _manager_loop = TransferManager(), loop
    return _manager


async def close_transfers():
    if _manager is not None:
        await _manager.close()


def safe_name(name, default):
    name = os.path.basename((name or default).replace("\\", "/"))
    if name in ("", ".", ".."):
        return default
    # Windows may also run this project; reject reserved filename characters.
    return "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)


def thumbnail_message(parent, thumbnail):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=parent.id, chat=parent.chat, _thumbnail_parent=parent,
        document=SimpleNamespace(file_id=thumbnail.file_id, file_size=thumbnail.file_size,
                                 file_name="thumb.jpg", mime_type="image/jpeg"),
    )


async def refreshed_source(source, message):
    parent = getattr(message, "_thumbnail_parent", None)
    fresh = await source.get_messages(message.chat.id, message.id)
    if parent is not None:
        thumbs = getattr(extract_media_info(fresh)[1], "thumbs", None) or []
        if not thumbs:
            raise ValueError("Source thumbnail disappeared during transfer")
        fresh = thumbnail_message(fresh, thumbs[0])
    old_id = pyrogram.file_id.FileId.decode(extract_media_info(message)[2])
    new_id = pyrogram.file_id.FileId.decode(extract_media_info(fresh)[2])
    if old_id.media_id != new_id.media_id:
        raise ValueError("Source media was replaced during transfer")
    return fresh


async def uploaded_media(bot, upload, manager):
    msg = upload.message
    media = extract_media_info(msg)[1]
    if upload.kind == "photo":
        return raw.types.InputMediaUploadedPhoto(file=upload.file, spoiler=getattr(msg, "has_media_spoiler", False))
    attributes = [raw.types.DocumentAttributeFilename(file_name=upload.name)]
    if upload.kind in ("video", "animation", "video_note"):
        attributes.append(raw.types.DocumentAttributeVideo(
            duration=getattr(media, "duration", 0), w=getattr(media, "width", 0),
            h=getattr(media, "height", 0), supports_streaming=True,
            round_message=upload.kind == "video_note",
        ))
        if upload.kind == "animation":
            attributes.append(raw.types.DocumentAttributeAnimated())
    elif upload.kind in ("audio", "voice"):
        attributes.append(raw.types.DocumentAttributeAudio(
            duration=getattr(media, "duration", 0), voice=upload.kind == "voice",
            title=getattr(media, "title", None), performer=getattr(media, "performer", None),
        ))
    elif upload.kind == "sticker":
        attributes.extend([
            raw.types.DocumentAttributeSticker(alt=getattr(media, "emoji", "") or "",
                                               stickerset=raw.types.InputStickerSetEmpty()),
            raw.types.DocumentAttributeImageSize(w=getattr(media, "width", 512), h=getattr(media, "height", 512)),
        ])
        if getattr(media, "is_animated", False):
            attributes.append(raw.types.DocumentAttributeAnimated())
    thumb = None
    thumbs = getattr(media, "thumbs", None) or []
    if thumbs and getattr(thumbs[0], "file_size", 0):
        # Thumbnails use the same memory/connection limits as full files.
        source_thumb = thumbs[0]
        thumb_message = thumbnail_message(msg, source_thumb)
        thumb_upload = await manager.prepare(upload.source, bot, thumb_message)
        upload.thumbnail = thumb_upload
        thumb = thumb_upload.file
    return raw.types.InputMediaUploadedDocument(
        file=upload.file, thumb=thumb, mime_type=upload.mime or "application/octet-stream",
        attributes=attributes, force_file=upload.kind == "document",
        spoiler=getattr(msg, "has_media_spoiler", False),
    )


async def caption(bot, message):
    return await utils.parse_text_entities(bot, message.caption or "", ParseMode.DISABLED,
                                         getattr(message, "caption_entities", None))


async def sent_messages(bot, response):
    messages = [update.message for update in response.updates if isinstance(update, (
        raw.types.UpdateNewMessage, raw.types.UpdateNewChannelMessage, raw.types.UpdateNewScheduledMessage
    ))]
    if not messages:
        raise RuntimeError("Telegram did not return a sent message")
    messages.sort(key=lambda message: message.id)
    return await utils.parse_messages(bot, raw.types.messages.Messages(
        messages=messages, topics=getattr(response, "topics", None) or [],
        users=response.users, chats=response.chats
    ))


async def copy_album(client, chat_id, source_chat, message_id):
    """Native file-ID copy, avoiding Pyrofork 2.3.69's topics parsing regression."""
    messages = sorted(await client.get_media_group(source_chat, message_id), key=lambda m: m.id)
    items = []
    for message in messages:
        encoded = extract_media_info(message)[2]
        items.append(raw.types.InputSingleMedia(
            media=utils.get_input_media_from_file_id(
                encoded, has_spoiler=getattr(message, "has_media_spoiler", False)),
            random_id=client.rnd_id(), **await caption(client, message)))
    query = raw.functions.messages.SendMultiMedia(
        peer=await client.resolve_peer(chat_id), multi_media=items)
    return await sent_messages(client, await final_request(client, query))


async def finalize(bot, query, uploads, manager):
    async def operation():
        for attempt in range(3):
            try:
                return await final_request(bot, query)
            except FilePartMissing as error:
                if attempt == 2:
                    raise
                # A single upload handle is used by SendMedia / UploadMedia.
                if len(uploads) != 1:
                    raise
                for upload in (uploads[0], uploads[0].thumbnail):
                    if upload is not None and error.value < upload.file.parts:
                        await manager.repair(bot, upload, error.value)
    return await operation()


async def relay_media(source, bot, message, target, progress_message=None, abort_event=None):
    async def operation():
        manager = get_manager()
        upload = await manager.prepare(source, bot, message, TransferProgress(progress_message))
        media = await uploaded_media(bot, upload, manager)
        query = raw.functions.messages.SendMedia(
            peer=await bot.resolve_peer(target), media=media, random_id=bot.rnd_id(),
            **await caption(bot, message),
        )
        await finalization_notice(progress_message)
        response = await finalize(bot, query, [upload], manager)
        return (await sent_messages(bot, response))[0]
    return await abortable(operation(), abort_event)


async def relay_album(source, bot, messages, target, progress_message=None, abort_event=None):
    async def operation():
        manager = get_manager()
        progress = TransferProgress(progress_message)
        if not 1 <= len(messages) <= 10:
            raise ValueError("An album must contain between one and ten media items")
        # Validate the whole album before beginning any downloads.
        for message in messages:
            manager.check_size(bot, extract_media_info(message)[3])
            key = (message.chat.id, message.id)
            progress.states[key] = (0, 0, extract_media_info(message)[3])
        peer = await bot.resolve_peer(target)
        async def prepare_item(message):
            upload = await manager.prepare(source, bot, message, progress)
            query = raw.functions.messages.UploadMedia(peer=peer, media=await uploaded_media(bot, upload, manager))
            result = await finalize(bot, query, [upload], manager)
            if upload.kind == "photo":
                entity = result.photo
                media = raw.types.InputMediaPhoto(id=raw.types.InputPhoto(
                    id=entity.id, access_hash=entity.access_hash, file_reference=entity.file_reference
                ), spoiler=getattr(message, "has_media_spoiler", False))
            else:
                entity = result.document
                media = raw.types.InputMediaDocument(id=raw.types.InputDocument(
                    id=entity.id, access_hash=entity.access_hash, file_reference=entity.file_reference
                ), spoiler=getattr(message, "has_media_spoiler", False))
            return raw.types.InputSingleMedia(media=media, random_id=bot.rnd_id(), **await caption(bot, message))
        items = await supervised([prepare_item(message) for message in messages])
        if len(items) == 1:
            item = items[0]
            query = raw.functions.messages.SendMedia(peer=peer, media=item.media, random_id=item.random_id,
                                                     message=item.message, entities=item.entities)
        else:
            query = raw.functions.messages.SendMultiMedia(peer=peer, multi_media=items)
        await finalization_notice(progress_message)
        response = await final_request(bot, query)
        return await sent_messages(bot, response)
    return await abortable(operation(), abort_event)
