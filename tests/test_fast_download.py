import os
os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("SESSION_STRING", "test-session-string")
os.environ.setdefault("FLOOD_WAIT_DELAY", "0")
os.environ.setdefault("BATCH_SIZE", "2")

import shutil
import asyncio
import tempfile
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.errors import FileReferenceExpired, AuthKeyDuplicated, FloodWait, AuthBytesInvalid
from pyrogram import raw
import pyrogram

from config import PyroConf
import main
from helpers.fast_download import (
    CHUNK_SIZE,
    MIN_PARALLEL_FILE_SIZE,
    extract_media_info,
    get_file_location,
    resolve_destination_path,
    fallback_download,
    fast_download,
    MTProtoWorkerSession,
)


class DummyMedia:
    def __init__(self, file_id, file_size, file_name=None, mime_type="video/mp4"):
        self.file_id = file_id
        self.file_size = file_size
        self.file_name = file_name
        self.mime_type = mime_type


class DummyMessage:
    def __init__(self, media_type="video", file_size=12 * 1024 * 1024, msg_id=999):
        self.id = msg_id
        fid = FileId(
            file_type=FileType.DOCUMENT,
            dc_id=2,
            media_id=54321,
            access_hash=98765,
            file_reference=b"ref123"
        )
        self.file_id_str = fid.encode()
        self.media_obj = DummyMedia(self.file_id_str, file_size, file_name=f"test_{msg_id}.mp4")
        setattr(self, media_type, self.media_obj)
        self.download_called = False
        self.download_kwargs = {}

    async def download(self, **kwargs):
        self.download_called = True
        self.download_kwargs = kwargs
        target = kwargs.get("file_name") or f"downloads/fallback_{self.id}.bin"
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        with open(target, "wb") as f:
            f.write(b"fallback_payload")
        return target


def make_dummy_client():
    client = MagicMock()
    client.is_connected = True
    storage = MagicMock()
    storage.test_mode = AsyncMock(return_value=False)
    storage.dc_id = AsyncMock(return_value=2)
    storage.auth_key = AsyncMock(return_value=b"k" * 256)
    client.storage = storage
    client.invoke = AsyncMock()
    return client


# --- 1. Configuration tests ---
def test_config_variables_exposed():
    assert hasattr(PyroConf, "PARALLEL_DOWNLOAD_WORKERS")
    assert PyroConf.PARALLEL_DOWNLOAD_WORKERS >= 1
    assert hasattr(PyroConf, "MAX_CONCURRENT_TRANSMISSIONS")
    bot_mct = getattr(main.bot, "max_concurrent_transmissions", main.bot.kwargs.get("max_concurrent_transmissions"))
    assert bot_mct == PyroConf.MAX_CONCURRENT_TRANSMISSIONS
    user_mct = getattr(main.user, "max_concurrent_transmissions", main.user.kwargs.get("max_concurrent_transmissions"))
    assert user_mct == PyroConf.MAX_CONCURRENT_TRANSMISSIONS


# --- 2. Media Info Extraction ---
def test_extract_media_info_various_types():
    # Video
    msg_video = DummyMessage(media_type="video", file_size=15 * 1024 * 1024)
    kind, media, fid, size, name, mime = extract_media_info(msg_video)
    assert kind == "video"
    assert media is msg_video.video
    assert fid == msg_video.file_id_str
    assert size == 15 * 1024 * 1024
    assert name == "test_999.mp4"
    assert mime == "video/mp4"

    # No media
    class EmptyMsg:
        id = 1
    kind, media, fid, size, name, mime = extract_media_info(EmptyMsg())
    assert kind is None
    assert media is None
    assert fid is None
    assert size == 0


# --- 3. Location Resolution ---
def test_get_file_location_types():
    # Document
    fid_doc = FileId(file_type=FileType.DOCUMENT, dc_id=2, media_id=10, access_hash=20, file_reference=b"ref")
    loc_doc = get_file_location(fid_doc)
    assert isinstance(loc_doc, raw.types.InputDocumentFileLocation)
    assert loc_doc.id == 10
    assert loc_doc.access_hash == 20
    assert loc_doc.file_reference == b"ref"

    # Photo
    fid_photo = FileId(file_type=FileType.PHOTO, dc_id=2, media_id=30, access_hash=40, file_reference=b"ref2", thumbnail_size="m")
    loc_photo = get_file_location(fid_photo)
    assert isinstance(loc_photo, raw.types.InputPhotoFileLocation)
    assert loc_photo.id == 30

    # Chat photo
    fid_chat = FileId(file_type=FileType.CHAT_PHOTO, dc_id=2, media_id=50, chat_id=123, chat_access_hash=456)
    loc_chat = get_file_location(fid_chat)
    assert isinstance(loc_chat, raw.types.InputPeerPhotoFileLocation)


# --- 4. Small File Fallback ---
@pytest.mark.asyncio
async def test_small_file_uses_standard_download():
    # 5MB is below MIN_PARALLEL_FILE_SIZE (10MB)
    msg = DummyMessage(media_type="document", file_size=5 * 1024 * 1024)
    client = make_dummy_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "small.bin")
        res = await fast_download(client, msg, file_name=dest_file)

        assert msg.download_called
        assert res == dest_file
        assert os.path.exists(dest_file)
        with open(dest_file, "rb") as f:
            assert f.read() == b"fallback_payload"


# --- 5. Unsupported Client Fallback ---
@pytest.mark.asyncio
async def test_unsupported_client_uses_fallback():
    # Large file (15MB), but client is not connected or lacks storage
    msg = DummyMessage(media_type="video", file_size=15 * 1024 * 1024)
    client = MagicMock()
    client.is_connected = False
    client.storage = None

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "fallback.bin")
        res = await fast_download(client, msg, file_name=dest_file)

        assert msg.download_called
        assert res == dest_file
        assert os.path.exists(dest_file)


# --- 6. Parallel Download Success ---
@pytest.mark.asyncio
async def test_parallel_download_success():
    # 12 MB file = exactly 12 chunks of 1MB
    file_size = 12 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    # Generate distinct 1MB chunks based on offset
    def mock_chunk(offset):
        header = f"CHUNK_OFFSET_{offset}".encode()
        return header + b"X" * (CHUNK_SIZE - len(header))

    progress_calls = []

    async def mock_progress(current, total):
        progress_calls.append((current, total))

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "large_output.mp4")

        # Mock MTProtoWorkerSession start and fetch_chunk
        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock) as mock_start, \
             patch.object(MTProtoWorkerSession, "fetch_chunk") as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock) as mock_stop:

            async def side_effect_fetch(location=None, offset_bytes=0, limit=0, *args, **kwargs):
                await asyncio.sleep(0.001)  # simulate async network I/O
                return mock_chunk(offset_bytes)

            mock_fetch.side_effect = side_effect_fetch

            result_path = await fast_download(
                client=client,
                message=msg,
                file_name=dest_file,
                progress=mock_progress,
                num_workers=3
            )

            assert result_path == dest_file
            assert os.path.exists(dest_file)
            assert os.path.getsize(dest_file) == file_size

            # Verify chunk content at every 1MB offset
            with open(dest_file, "rb") as fh:
                for chunk_idx in range(12):
                    offset = chunk_idx * CHUNK_SIZE
                    fh.seek(offset)
                    data = fh.read(CHUNK_SIZE)
                    expected = mock_chunk(offset)
                    assert data == expected, f"Mismatch at chunk {chunk_idx}"

            # Verify progress callbacks were invoked and reached total
            assert len(progress_calls) >= 12
            assert progress_calls[-1] == (file_size, file_size)

            # Verify worker sessions were stopped cleanly
            assert mock_stop.call_count == 3
            # Standard download was NOT called
            assert not msg.download_called


# --- 7. Cancellation via abort_event before start ---
@pytest.mark.asyncio
async def test_cancellation_abort_event_before_start():
    msg = DummyMessage(media_type="video", file_size=15 * 1024 * 1024)
    client = make_dummy_client()
    abort_event = asyncio.Event()
    abort_event.set()

    with pytest.raises(asyncio.CancelledError):
        await fast_download(client, msg, abort_event=abort_event)


# --- 8. Cancellation during download ---
@pytest.mark.asyncio
async def test_cancellation_during_download():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()
    abort_event = asyncio.Event()

    chunk_counter = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "cancelled.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk") as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            async def side_effect_fetch(location=None, offset_bytes=0, limit=0, *args, **kwargs):
                nonlocal chunk_counter
                chunk_counter += 1
                if chunk_counter >= 2:
                    abort_event.set()
                await asyncio.sleep(0.01)
                return b"A" * CHUNK_SIZE

            mock_fetch.side_effect = side_effect_fetch

            with pytest.raises(asyncio.CancelledError):
                await fast_download(client, msg, file_name=dest_file, abort_event=abort_event, num_workers=2)

            # Temporary file must be cleaned up
            assert not os.path.exists(dest_file + ".temp")
            assert not os.path.exists(dest_file)


# --- 9. FileReferenceExpired Propagates ---
@pytest.mark.asyncio
async def test_file_reference_expired_propagates():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "expired.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk") as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            mock_fetch.side_effect = FileReferenceExpired()

            with pytest.raises(FileReferenceExpired):
                await fast_download(client, msg, file_name=dest_file, num_workers=2)

            assert not os.path.exists(dest_file + ".temp")


# --- 10. AuthKeyDuplicated Propagates ---
@pytest.mark.asyncio
async def test_auth_key_duplicated_propagates():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "auth_dup.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk") as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            mock_fetch.side_effect = AuthKeyDuplicated()

            with pytest.raises(AuthKeyDuplicated):
                await fast_download(client, msg, file_name=dest_file, num_workers=2)


# --- 11. Worker Crash Safely Falls Back to Standard Download ---
@pytest.mark.asyncio
async def test_worker_crash_falls_back_safely():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "crashed.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk") as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            # Simulate network/session failure during chunk download
            mock_fetch.side_effect = ConnectionResetError("Telegram MTProto connection dropped")

            res = await fast_download(client, msg, file_name=dest_file, num_workers=2)

            # Safe fallback was triggered
            assert msg.download_called
            assert res == dest_file
            assert os.path.exists(dest_file)
            with open(dest_file, "rb") as f:
                assert f.read() == b"fallback_payload"


# --- 12. Path Resolution ---
def test_resolve_destination_path():
    # Explicit file path
    p1 = resolve_destination_path(MagicMock(id=10), "downloads/custom.mp4", "video", None)
    assert p1.endswith("custom.mp4")

    # Directory path
    p2 = resolve_destination_path(MagicMock(id=20), "downloads/", "video", "orig.mp4")
    assert p2.endswith("orig.mp4")

    # Default None
    p3 = resolve_destination_path(MagicMock(id=30), None, "audio", None)
    assert p3.endswith("audio_30.mp3")


# --- 13. StopTransmission cleanly cancels download ---
@pytest.mark.asyncio
async def test_stop_transmission_cancels_cleanly():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    async def stopping_progress(current, total):
        if current > 0:
            raise pyrogram.StopTransmission()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "stopped.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk", new_callable=AsyncMock) as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            mock_fetch.return_value = b"A" * CHUNK_SIZE

            res = await fast_download(
                client=client,
                message=msg,
                file_name=dest_file,
                progress=stopping_progress,
                num_workers=2
            )

            assert res is None
            assert not os.path.exists(dest_file)


# --- 14. Chunk Size Mismatch Triggers Fallback ---
@pytest.mark.asyncio
async def test_chunk_size_mismatch_triggers_fallback():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "mismatch.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk", new_callable=AsyncMock) as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            # Return truncated chunk (500 bytes instead of 1MB)
            mock_fetch.return_value = b"A" * 500

            res = await fast_download(client, msg, file_name=dest_file, num_workers=2)

            assert msg.download_called
            assert res == dest_file
            assert os.path.exists(dest_file)
            with open(dest_file, "rb") as f:
                assert f.read() == b"fallback_payload"


# --- 15. Incomplete Download Triggers Fallback ---
@pytest.mark.asyncio
async def test_incomplete_download_triggers_fallback():
    file_size = 15 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "incomplete.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk", new_callable=AsyncMock) as mock_fetch, \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            # Drain queue immediately or return early
            mock_fetch.return_value = b"A" * CHUNK_SIZE

            with patch("helpers.fast_download.CHUNK_SIZE", 2 * CHUNK_SIZE):
                res = await fast_download(client, msg, file_name=dest_file, num_workers=2)

            assert msg.download_called
            assert res == dest_file


# --- 16. Foreign DC AuthBytesInvalid Retry ---
@pytest.mark.asyncio
async def test_foreign_dc_auth_bytes_invalid_retry():
    client = make_dummy_client()
    # Main DC is 2, worker session connects to foreign DC 4
    client.storage.dc_id = AsyncMock(return_value=2)

    worker = MTProtoWorkerSession(client, dc_id=4)

    mock_session = MagicMock()
    mock_session.start = AsyncMock()
    mock_session.stop = AsyncMock()

    call_count = 0
    async def mock_import_auth(query):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AuthBytesInvalid("Invalid auth bytes on first attempt")
        return MagicMock()

    mock_session.invoke = AsyncMock(side_effect=mock_import_auth)

    with patch("helpers.fast_download.Session", return_value=mock_session), \
         patch("helpers.fast_download.Auth") as mock_auth_cls:

        mock_auth_instance = MagicMock()
        mock_auth_instance.create = AsyncMock(return_value=b"new_key" * 32)
        mock_auth_cls.return_value = mock_auth_instance

        # ExportAuthorization mock return
        client.invoke.return_value = MagicMock(id=1, bytes=b"auth_bytes")

        await worker.start()

        # Confirms it retried and invoked ImportAuthorization twice
        assert call_count == 2
        assert mock_session.start.call_count == 1


# --- 17. Transient Chunk Error Retries Successfully ---
@pytest.mark.asyncio
async def test_transient_chunk_error_retries_successfully():
    file_size = 12 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    call_count = 0
    async def transient_fetch(location=None, offset_bytes=0, limit=0, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        # First chunk fetch fails with timeout once, then succeeds
        if call_count == 1:
            raise TimeoutError("Temporary MTProto timeout")
        await asyncio.sleep(0.001)
        return b"Z" * CHUNK_SIZE

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "transient_ok.mp4")

        with patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk", side_effect=transient_fetch), \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            res = await fast_download(client, msg, file_name=dest_file, num_workers=2)

            # Did NOT fall back because retry succeeded!
            assert not msg.download_called
            assert res == dest_file
            assert os.path.exists(dest_file)
            assert os.path.getsize(dest_file) == file_size


# --- 18. Asymmetric Crash Cancels Peer Workers Cleanly ---
@pytest.mark.asyncio
async def test_asymmetric_crash_cancels_peer_workers():
    file_size = 12 * CHUNK_SIZE
    msg = DummyMessage(media_type="video", file_size=file_size)
    client = make_dummy_client()

    captured_tasks = []
    orig_create_task = asyncio.create_task

    def track_task(coro):
        t = orig_create_task(coro)
        captured_tasks.append(t)
        return t

    async def failing_fetch(location=None, offset_bytes=0, limit=0, *args, **kwargs):
        if offset_bytes == 0:
            # Fatal error on worker 1 after all retries
            raise ConnectionResetError("Fatal connection reset")
        # Worker 2 is slow
        await asyncio.sleep(1.0)
        return b"X" * CHUNK_SIZE

    with tempfile.TemporaryDirectory() as tmpdir:
        dest_file = os.path.join(tmpdir, "asym_test.mp4")

        with patch("asyncio.create_task", side_effect=track_task), \
             patch.object(MTProtoWorkerSession, "start", new_callable=AsyncMock), \
             patch.object(MTProtoWorkerSession, "fetch_chunk", side_effect=failing_fetch), \
             patch.object(MTProtoWorkerSession, "stop", new_callable=AsyncMock):

            res = await fast_download(client, msg, file_name=dest_file, num_workers=2)

            # Fallback was executed
            assert msg.download_called
            assert res == dest_file

            # Verify that peer worker tasks were cancelled and are not dangling
            assert len(captured_tasks) == 2
            for t in captured_tasks:
                assert t.done()
                if not t.cancelled():
                    exc = t.exception()
                    if exc:
                        assert isinstance(exc, ConnectionResetError)

