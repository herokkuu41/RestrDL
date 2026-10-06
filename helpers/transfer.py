"""Bounded Telegram-to-Telegram relay. No whole-file buffering on the fast path."""

import asyncio
import hashlib
import math
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pyrogram
from pyrogram import raw, utils
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    AuthKeyDuplicated, FilePartMissing, FileReferenceExpired, FileReferenceInvalid,
    FloodWait, InternalServerError, ServiceUnavailable,
)

from config import PyroConf
from helpers.fast_download import CHUNK_SIZE, MTProtoWorkerSession, extract_media_info, get_file_location
from logger import LOGGER

PART_SIZE = 512 * 1024
TRANSIENT = (OSError, TimeoutError, InternalServerError, ServiceUnavailable)
REFERENCES = (FileReferenceExpired, FileReferenceInvalid)


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
    """Fixed global slots, including cached idle sessions across clients/DCs."""
    def __init__(self, count):
        self.available = asyncio.Queue()
        self.sessions = [None] * count
        self.keys = [None] * count
        self.auth_lock = asyncio.Lock()
        self.native_lock = asyncio.Lock()
        for index in range(count):
            self.available.put_nowait(index)

    @asynccontextmanager
    async def lease(self, client, dc):
        index = await self.available.get()
        try:
            if self.keys[index] != (client, dc):
                if self.sessions[index] is not None:
                    await self.sessions[index].stop()
                self.sessions[index] = None
                self.keys[index] = None
                worker = MTProtoWorkerSession(client, dc)
                try:
                    # Only foreign authorization must be serialized; same-DC
                    # connections can initialize concurrently.
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
            yield self.sessions[index]
        except (OSError, TimeoutError, AuthKeyDuplicated, asyncio.CancelledError):
            if self.sessions[index] is not None:
                await self.sessions[index].stop()
            self.sessions[index] = self.keys[index] = None
            raise
        finally:
            self.available.put_nowait(index)

    async def close(self):
        await supervised([worker.stop() for worker in self.sessions if worker is not None])
        self.sessions[:] = [None] * len(self.sessions)
        self.keys[:] = [None] * len(self.keys)

    @asynccontextmanager
    async def native(self):
        # Pyrofork's CDN iterator opens both an origin and a CDN session.
        # Reserve their slots atomically to avoid two iterators waiting on each other.
        if len(self.sessions) < 2:
            raise ValueError("CDN streaming requires at least two download connection slots")
        async with self.native_lock:
            indices = []
            try:
                for _ in range(2):
                    index = await self.available.get()
                    indices.append(index)
                    if self.sessions[index] is not None:
                        await self.sessions[index].stop()
                    self.sessions[index] = self.keys[index] = None
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
        self.started = time.monotonic()
        self.last_edit = self.started
        self.states = {}
        self.lock = asyncio.Lock()

    async def __call__(self, key, downloaded, uploaded, total):
        self.states[key] = (downloaded, uploaded, total)
        if self.message is None:
            return
        async with self.lock:
            now = time.monotonic()
            if now - self.last_edit < 5:
                return
            self.last_edit = now
            down, up, size = map(sum, zip(*self.states.values()))
            elapsed = max(now - self.started, 0.001)
            text = (
                f"**Transferring**\n"
                f"Downloaded: {down / 1048576:.1f}/{size / 1048576:.1f} MiB "
                f"({down / elapsed / 1048576:.2f} MiB/s)\n"
                f"Uploaded (confirmed): {up / 1048576:.1f}/{size / 1048576:.1f} MiB "
                f"({up / elapsed / 1048576:.2f} MiB/s)\nElapsed: {elapsed:.0f}s"
            )
            try:
                await self.message.edit(text)
            except (FloodWait, AuthKeyDuplicated, pyrogram.StopTransmission):
                raise
            except Exception as error:
                LOGGER(__name__).debug("Progress edit failed: %s", error)


class TransferManager:
    def __init__(self, downloads=None, uploads=None, active=None, buffer_mib=None):
        self.downloads = ConnectionPool(downloads or PyroConf.PARALLEL_DOWNLOAD_WORKERS)
        self.uploads = ConnectionPool(uploads or PyroConf.PARALLEL_UPLOAD_WORKERS)
        self.active = asyncio.Semaphore(active or PyroConf.MAX_ACTIVE_TRANSFERS)
        self.budget = ByteBudget((buffer_mib or PyroConf.TRANSFER_BUFFER_MIB) * 1048576)
        self.disk_lock = asyncio.Lock()
        self.disk_reserved = 0

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
                response = await worker.session.invoke(raw.functions.upload.GetFile(
                    location=get_file_location(fid), offset=offset, limit=CHUNK_SIZE
                ), sleep_threshold=30)
            if isinstance(response, raw.types.upload.FileCdnRedirect):
                raise CdnRedirect()
            if not isinstance(response, raw.types.upload.File) or len(response.bytes) != expected:
                raise ValueError(f"Invalid download chunk at {offset}: expected {expected} bytes")
            return response.bytes
        return await retry(operation)

    async def put_part(self, bot, file_id, part, count, size, data):
        dc = await bot.storage.dc_id()
        query = (raw.functions.upload.SaveBigFilePart(
            file_id=file_id, file_part=part, file_total_parts=count, bytes=data
        ) if size > 10 * 1048576 else raw.functions.upload.SaveFilePart(
            file_id=file_id, file_part=part, bytes=data
        ))
        async def operation():
            async with self.uploads.lease(bot, dc) as worker:
                result = await worker.session.invoke(query, sleep_threshold=30)
            if result is not True:
                raise OSError(f"Telegram did not acknowledge upload part {part}")
        await retry(operation)

    async def prepare(self, source, bot, message, progress=None, abort_event=None):
        return await abortable(self._prepare(source, bot, message, progress), abort_event)

    async def _prepare(self, source, bot, message, progress):
        kind, media, encoded, size, name, mime = extract_media_info(message)
        self.check_size(bot, size)
        extensions = {"photo": ".jpg", "video": ".mp4", "animation": ".mp4", "video_note": ".mp4",
                      "audio": ".mp3", "voice": ".ogg"}
        name = safe_name(name, f"{kind}_{message.id}{extensions.get(kind, '.bin')}")
        async with self.active:
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
        queue = asyncio.Queue(16)
        credits = set()
        pending = []
        file_id = bot.rnd_id()
        count = math.ceil(size / PART_SIZE)
        digest = hashlib.md5()
        downloaded = uploaded = 0
        key = (getattr(getattr(message, "chat", None), "id", None), message.id)

        async def notify():
            if progress:
                await progress(key, downloaded, uploaded, size)

        async def chunk(offset):
            nonlocal downloaded
            expected = min(CHUNK_SIZE, size - offset)
            await self.budget.acquire(2 * expected)
            credit = Credit(self.budget, 2 * expected, math.ceil(expected / PART_SIZE))
            credits.add(credit)
            data = await self.fetch(source, fid, offset, expected)
            downloaded += len(data)
            await notify()
            return offset, data, credit

        async def enqueue(offset, data, credit):
            if size <= 10 * 1048576:
                digest.update(data)  # Producer consumes chunks in source order.
            for start in range(0, len(data), PART_SIZE):
                await queue.put(((offset + start) // PART_SIZE, data[start:start + PART_SIZE], credit))

        async def produce():
            nonlocal downloaded
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
                        await notify()
                        await enqueue(offset, data, credit)
                        offset += len(data)
                    # Do not retain the final data buffer while waiting for acknowledgements.
                    data = None
                finally:
                    await stream.aclose()
            else:
                offsets = iter(range(0, size, CHUNK_SIZE))
                try:
                    for _ in range(len(self.downloads.sessions)):
                        offset = next(offsets, None)
                        if offset is not None:
                            pending.append(asyncio.create_task(chunk(offset)))
                    while pending:
                        task = pending.pop(0)
                        try:
                            offset, data, credit = await task
                        finally:
                            if not task.done():
                                task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        await enqueue(offset, data, credit)
                        data = None
                        task = None
                        following = next(offsets, None)
                        if following is not None:
                            pending.append(asyncio.create_task(chunk(following)))
                finally:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            for _ in self.uploads.sessions:
                await queue.put(None)

        async def consume():
            nonlocal uploaded
            while True:
                item = await queue.get()
                if item is None:
                    return
                part, data, credit = item
                await self.put_part(bot, file_id, part, count, size, data)
                uploaded += len(data)
                await credit.acknowledged()
                if not credit.amount:
                    credits.discard(credit)
                data = item = None
                await notify()

        try:
            if native:
                async with self.downloads.native():
                    await supervised([produce(), *(consume() for _ in self.uploads.sessions)])
            else:
                await supervised([produce(), *(consume() for _ in self.uploads.sessions)])
            if downloaded != size or uploaded != size:
                raise ValueError("Transfer completed with missing bytes")
            LOGGER(__name__).info("Relay complete: %s bytes, buffer peak %s bytes", size, self.budget.peak)
            if size > 10 * 1048576:
                return raw.types.InputFileBig(id=file_id, parts=count, name=name)
            return raw.types.InputFile(id=file_id, parts=count, name=name, md5_checksum=digest.hexdigest())
        finally:
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
            await self.put_part(bot, upload.file.id, part, upload.file.parts, upload.size, data[start:start + PART_SIZE])
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
    return await utils.parse_messages(bot, raw.types.messages.Messages(
        messages=messages, users=response.users, chats=response.chats
    ))


async def finalize(bot, query, uploads, manager):
    async def operation():
        for attempt in range(3):
            try:
                return await retry(lambda: bot.invoke(query))
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
        response = await retry(lambda: bot.invoke(query))
        return await sent_messages(bot, response)
    return await abortable(operation(), abort_event)
