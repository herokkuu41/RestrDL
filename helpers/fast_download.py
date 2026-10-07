import os
import math
import shutil
import asyncio
import inspect
import uuid
from typing import Optional, Callable, Tuple, Any

import pyrogram
from pyrogram import raw, utils
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Session, Auth
from pyrogram.errors import FileReferenceExpired, FloodWait, AuthKeyDuplicated, AuthBytesInvalid

from config import PyroConf
from logger import LOGGER

# Chunk size for parallel download: 1MB (divisible by 4KB, max per MTProto GetFile)
CHUNK_SIZE = 1024 * 1024  # 1 MB
# Minimum file size to trigger parallel multi-session download (10MB)
MIN_PARALLEL_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def extract_media_info(message: Any) -> Tuple[Optional[str], Optional[Any], Optional[str], int, Optional[str], str]:
    """Extract media type, media object, file_id string, file_size, file_name, and mime_type from a message."""
    media_attrs = (
        "document",
        "video",
        "audio",
        "photo",
        "voice",
        "video_note",
        "animation",
        "sticker",
    )
    for kind in media_attrs:
        media = getattr(message, kind, None)
        if media is not None:
            file_id_str = getattr(media, "file_id", None)
            file_size = getattr(media, "file_size", 0) or 0
            if not file_size and kind == "photo" and hasattr(media, "sizes") and media.sizes:
                file_size = getattr(media.sizes[-1], "file_size", 0) or 0
            file_name = getattr(media, "file_name", None)
            mime_type = getattr(media, "mime_type", "") or ""
            return kind, media, file_id_str, file_size, file_name, mime_type
    return None, None, None, 0, None, ""


def get_file_location(file_id_obj: FileId) -> Any:
    """Build the raw MTProto InputFileLocation from a decoded FileId object."""
    file_type = file_id_obj.file_type

    if file_type == FileType.CHAT_PHOTO:
        if file_id_obj.chat_id > 0:
            peer = raw.types.InputPeerUser(
                user_id=file_id_obj.chat_id,
                access_hash=file_id_obj.chat_access_hash
            )
        else:
            if file_id_obj.chat_access_hash == 0:
                peer = raw.types.InputPeerChat(chat_id=-file_id_obj.chat_id)
            else:
                peer = raw.types.InputPeerChannel(
                    channel_id=utils.get_channel_id(file_id_obj.chat_id),
                    access_hash=file_id_obj.chat_access_hash
                )

        return raw.types.InputPeerPhotoFileLocation(
            peer=peer,
            photo_id=file_id_obj.media_id,
            big=file_id_obj.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG
        )
    elif file_type == FileType.PHOTO:
        return raw.types.InputPhotoFileLocation(
            id=file_id_obj.media_id,
            access_hash=file_id_obj.access_hash,
            file_reference=file_id_obj.file_reference,
            thumb_size=file_id_obj.thumbnail_size
        )
    else:
        return raw.types.InputDocumentFileLocation(
            id=file_id_obj.media_id,
            access_hash=file_id_obj.access_hash,
            file_reference=file_id_obj.file_reference,
            thumb_size=file_id_obj.thumbnail_size
        )


async def fallback_download(
    message: Any,
    file_name: Optional[str] = None,
    progress: Optional[Callable] = None,
    progress_args: tuple = (),
    abort_event: Optional[asyncio.Event] = None
) -> Optional[str]:
    """Safely fall back to Pyrogram's native message.download() method."""
    if abort_event and abort_event.is_set():
        raise asyncio.CancelledError("Download aborted before fallback started")

    kwargs = {}
    if file_name is not None:
        kwargs["file_name"] = file_name
    if progress is not None:
        kwargs["progress"] = progress
    if progress_args:
        kwargs["progress_args"] = progress_args

    return await message.download(**kwargs)


class MTProtoWorkerSession:
    """Manages an isolated MTProto DC media session for parallel chunk downloading."""

    def __init__(self, client: Any, dc_id: int):
        self.client = client
        self.dc_id = dc_id
        self.session: Optional[Session] = None

    async def start(self) -> None:
        storage = getattr(self.client, "storage", None)
        if not storage:
            raise RuntimeError("Client storage is not available")

        test_mode = await storage.test_mode()
        main_dc = await storage.dc_id()
        cache = getattr(self, "authorization_cache", None)
        cache_key = (id(self.client), self.dc_id, test_mode)
        cached = cache.get(cache_key) if cache is not None else None

        if self.dc_id == main_dc:
            auth_key = await storage.auth_key()
        elif cached is not None:
            auth_key = cached
        else:
            auth_key = await Auth(self.client, self.dc_id, test_mode).create()

        self.session = Session(
            self.client,
            self.dc_id,
            auth_key,
            test_mode,
            is_media=True
        )
        await self.session.start()

        if self.dc_id != main_dc and cached is None:
            for _ in range(3):
                exported_auth = await self.client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=self.dc_id)
                )
                try:
                    await self.session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported_auth.id,
                            bytes=exported_auth.bytes
                        )
                    )
                except AuthBytesInvalid:
                    continue
                else:
                    break
            else:
                await self.session.stop()
                raise AuthBytesInvalid("Failed to import authorization after 3 attempts")
            if cache is not None:
                cache[cache_key] = auth_key

    async def fetch_chunk(self, location: Any, offset_bytes: int, limit: int) -> bytes:
        if not self.session:
            raise RuntimeError("MTProto worker session not started")

        r = await self.session.invoke(
            raw.functions.upload.GetFile(
                location=location,
                offset=offset_bytes,
                limit=limit
            ),
            sleep_threshold=30
        )

        if isinstance(r, raw.types.upload.File):
            return r.bytes
        elif isinstance(r, raw.types.upload.FileCdnRedirect):
            raise RuntimeError("CDN redirect encountered; fallback required")
        else:
            raise RuntimeError(f"Unexpected MTProto response type: {type(r)}")

    async def stop(self) -> None:
        if self.session:
            try:
                await self.session.stop()
            except Exception:
                pass
            self.session = None


def resolve_destination_path(message: Any, file_name: Optional[str], media_kind: str, original_file_name: Optional[str]) -> str:
    """Resolve the final absolute download file path."""
    ext = ".bin"
    if media_kind == "photo":
        ext = ".jpg"
    elif media_kind in ("video", "animation", "video_note"):
        ext = ".mp4"
    elif media_kind == "audio":
        ext = ".mp3"
    elif media_kind == "voice":
        ext = ".ogg"

    if file_name:
        dest_path = os.path.abspath(file_name)
        if os.path.isdir(dest_path) or file_name.endswith("/") or file_name.endswith("\\"):
            base_name = original_file_name or f"{media_kind}_{getattr(message, 'id', 'media')}{ext}"
            dest_path = os.path.join(dest_path, base_name)
    else:
        directory = os.path.abspath("downloads")
        base_name = original_file_name or f"{media_kind}_{getattr(message, 'id', 'media')}{ext}"
        dest_path = os.path.join(directory, base_name)

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    return dest_path


async def fast_download(
    client: Optional[Any],
    message: Any,
    file_name: Optional[str] = None,
    progress: Optional[Callable] = None,
    progress_args: tuple = (),
    abort_event: Optional[asyncio.Event] = None,
    num_workers: Optional[int] = None
) -> Optional[str]:
    """Download Telegram media using parallel MTProto chunk downloader.

    For media files >= 10MB, downloads 1MB chunks concurrently using multiple MTProto
    media sessions. Falls back safely to standard message.download() for smaller files,
    unsupported clients, or if any unexpected MTProto failure occurs.
    """
    # 1. Immediate cancellation check
    if abort_event and abort_event.is_set():
        raise asyncio.CancelledError("Download aborted by abort_event")

    # 2. Extract media and size information
    kind, media, file_id_str, file_size, orig_filename, _ = extract_media_info(message)
    if not media or not file_id_str:
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    # 3. For small files (< 10MB) or unspecified size, use standard download
    if file_size < MIN_PARALLEL_FILE_SIZE:
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    # 4. Resolve client instance
    if client is None:
        client = getattr(message, "_client", None)
    if client is None:
        try:
            import main
            client = getattr(main, "user", None)
        except Exception:
            pass

    # Validate that client has MTProto storage & connection capabilities
    if not client or not getattr(client, "storage", None) or not getattr(client, "is_connected", False):
        LOGGER(__name__).debug("Client does not support raw MTProto sessions; falling back to standard download.")
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    # 5. Decode FileId and construct MTProto location
    try:
        file_id_obj = FileId.decode(file_id_str)
        location = get_file_location(file_id_obj)
        dc_id = file_id_obj.dc_id
    except Exception as e:
        LOGGER(__name__).warning(f"Could not parse FileId for parallel download ({e}); falling back.")
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    # 6. Calculate chunk distribution and worker count
    worker_limit = num_workers if num_workers is not None else getattr(PyroConf, "PARALLEL_DOWNLOAD_WORKERS", 3)
    if not worker_limit or worker_limit <= 1:
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    total_chunks = math.ceil(file_size / CHUNK_SIZE)
    active_worker_count = min(worker_limit, total_chunks)

    if active_worker_count <= 1:
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    dest_path = resolve_destination_path(message, file_name, kind, orig_filename)
    unique_suffix = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
    temp_path = f"{dest_path}.{unique_suffix}.temp"

    # 7. Attempt parallel download across MTProto sessions
    workers = []
    file_handle = None
    queue = asyncio.Queue()
    for chunk_idx in range(total_chunks):
        offset = chunk_idx * CHUNK_SIZE
        queue.put_nowait((chunk_idx, offset))

    downloaded_bytes = 0
    write_lock = asyncio.Lock()
    progress_lock = asyncio.Lock()
    completed_successfully = False
    worker_tasks = []
    start_tasks = []

    async def stop_workers():
        for task in start_tasks + worker_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(start_tasks + worker_tasks), return_exceptions=True)
        await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)

    try:
        # Pre-allocate sparse/zeroed temporary file
        with open(temp_path, "wb") as f_init:
            f_init.truncate(file_size)

        file_handle = open(temp_path, "r+b")

        # Initialize parallel MTProto DC worker sessions
        LOGGER(__name__).info(
            f"Starting parallel download: {active_worker_count} workers, {total_chunks} chunks (Size: {file_size} bytes, DC: {dc_id})"
        )
        for _ in range(active_worker_count):
            w = MTProtoWorkerSession(client, dc_id)
            workers.append(w)

        start_tasks = [asyncio.create_task(w.start()) for w in workers]
        try:
            await asyncio.gather(*start_tasks)
        finally:
            for task in start_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*start_tasks, return_exceptions=True)

        async def worker_loop(worker: MTProtoWorkerSession):
            nonlocal downloaded_bytes
            while not queue.empty():
                if abort_event and abort_event.is_set():
                    raise asyncio.CancelledError("Aborted by abort_event")

                try:
                    chunk_idx, offset_bytes = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                expected_bytes = min(CHUNK_SIZE, file_size - offset_bytes)
                if expected_bytes <= 0:
                    queue.task_done()
                    continue

                max_chunk_retries = 2
                chunk_bytes = None
                for attempt in range(max_chunk_retries + 1):
                    if abort_event and abort_event.is_set():
                        raise asyncio.CancelledError("Aborted by abort_event")
                    try:
                        chunk_bytes = await worker.fetch_chunk(
                            location=location,
                            offset_bytes=offset_bytes,
                            limit=CHUNK_SIZE
                        )
                        break
                    except (FileReferenceExpired, AuthKeyDuplicated, FloodWait, asyncio.CancelledError):
                        raise
                    except Exception as chunk_err:
                        if attempt == max_chunk_retries:
                            raise
                        LOGGER(__name__).warning(
                            f"Worker fetch chunk {chunk_idx} failed (attempt {attempt+1}/{max_chunk_retries+1}): {chunk_err}. Retrying..."
                        )
                        await asyncio.sleep(0.5)

                if abort_event and abort_event.is_set():
                    raise asyncio.CancelledError("Aborted by abort_event")

                if len(chunk_bytes) != expected_bytes:
                    raise RuntimeError(
                        f"Chunk {chunk_idx} size mismatch: expected {expected_bytes} bytes, received {len(chunk_bytes)} bytes"
                    )

                async with write_lock:
                    if file_handle is not None and not file_handle.closed:
                        file_handle.seek(offset_bytes)
                        file_handle.write(chunk_bytes)
                        file_handle.flush()

                downloaded_bytes += len(chunk_bytes)

                if progress:
                    async with progress_lock:
                        try:
                            res = progress(downloaded_bytes, file_size, *progress_args)
                            if inspect.iscoroutine(res):
                                await res
                        except pyrogram.StopTransmission:
                            raise
                        except Exception as pe:
                            LOGGER(__name__).debug(f"Progress error: {pe}")

                queue.task_done()

        worker_tasks = [asyncio.create_task(worker_loop(w)) for w in workers]
        try:
            await asyncio.gather(*worker_tasks)
        finally:
            for t in worker_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)

        if downloaded_bytes != file_size:
            raise RuntimeError(
                f"Incomplete download: received {downloaded_bytes} bytes, expected {file_size} bytes"
            )

        # Final progress notification
        if progress:
            async with progress_lock:
                try:
                    res = progress(file_size, file_size, *progress_args)
                    if inspect.iscoroutine(res):
                        await res
                except pyrogram.StopTransmission:
                    raise
                except Exception:
                    pass

        # Close file before moving
        file_handle.close()
        file_handle = None

        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception:
                pass
        shutil.move(temp_path, dest_path)
        completed_successfully = True

        LOGGER(__name__).info(f"Parallel download completed: {dest_path} ({os.path.getsize(dest_path)} bytes)")
        return dest_path

    except (FileReferenceExpired, AuthKeyDuplicated, FloodWait, asyncio.CancelledError) as e:
        # Crucial errors that caller must handle or propagate
        raise e
    except pyrogram.StopTransmission:
        LOGGER(__name__).info("Download stopped via pyrogram.StopTransmission in progress callback.")
        return None
    except Exception as exc:
        LOGGER(__name__).warning(
            f"Parallel download failed ({type(exc).__name__}: {exc}). Falling back to standard download."
        )
        if file_handle:
            try:
                file_handle.close()
            except Exception:
                pass
            file_handle = None
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        await stop_workers()
        return await fallback_download(message, file_name, progress, progress_args, abort_event)

    finally:
        if file_handle:
            try:
                file_handle.close()
            except Exception:
                pass
        if not completed_successfully and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        # Stop all worker sessions cleanly
        await stop_workers()
