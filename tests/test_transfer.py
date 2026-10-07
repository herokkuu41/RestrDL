"""Network-free protocol tests against the pinned Pyrofork raw types."""
import asyncio
import hashlib
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from pyrogram import raw
from pyrogram.file_id import FileId, FileType
from pyrogram.errors import FilePartMissing, FileReferenceExpired, FloodWait, FloodPremiumWait, AuthKeyDuplicated

import helpers.transfer as transfer
from helpers.transfer import CHUNK_SIZE, PART_SIZE, TransferManager, Upload


def message(size, identity=1, kind="document"):
    fid = FileId(file_type=FileType.DOCUMENT, dc_id=2, media_id=identity,
                 access_hash=123, file_reference=b"ref")
    media = NS(file_id=fid.encode(), file_size=size, file_name=f"file_{identity}.bin",
               mime_type="application/octet-stream", thumbs=[])
    return NS(id=identity, chat=NS(id=-100123), **{kind: media}, caption="caption",
              caption_entities=None, has_media_spoiler=False)


def clients():
    source = NS(role="source", storage=NS(dc_id=AsyncMock(return_value=2)))
    bot = NS(role="bot", storage=NS(dc_id=AsyncMock(return_value=2)), me=NS(is_premium=False))
    ids = iter(range(1000, 100000))
    bot.rnd_id = lambda: next(ids)
    bot.resolve_peer = AsyncMock(return_value=raw.types.InputPeerSelf())
    bot.parser = NS(parse=AsyncMock(return_value={"message": "caption", "entities": None}))
    return source, bot


class Backend:
    def __init__(self, monkeypatch):
        self.parts = defaultdict(dict)
        self.downloaded = []
        self.uploaded_before_finished = False
        self.events = []
        self.live = defaultdict(int)
        self.peak = defaultdict(int)
        self.active_rpc = defaultdict(int)
        self.rpc_peak = defaultdict(int)
        self.size = {}
        self.delay = 0.001
        self.error = None
        self.false_ack = False
        self.cdn = False
        self.gate = None
        backend = self

        class Worker:
            def __init__(self, client, dc):
                self.client, self.dc = client, dc
                self.session = self
                self.started = False

            async def start(self):
                self.started = True
                backend.live[self.client.role] += 1
                backend.peak[self.client.role] = max(backend.peak[self.client.role], backend.live[self.client.role])

            async def stop(self):
                if self.started:
                    backend.live[self.client.role] -= 1
                    self.started = False

            async def invoke(self, query, **kwargs):
                role = self.client.role
                backend.active_rpc[role] += 1
                backend.rpc_peak[role] = max(backend.rpc_peak[role], backend.active_rpc[role])
                try:
                    await asyncio.sleep(backend.delay)
                    if isinstance(query, raw.functions.upload.GetFile):
                        if backend.cdn:
                            return raw.types.upload.FileCdnRedirect(dc_id=5, file_token=b"t", encryption_key=b"k"*32,
                                encryption_iv=b"i"*16, file_hashes=[])
                        if backend.error:
                            error, backend.error = backend.error, None
                            raise error
                        size = backend.size[query.location.id]
                        data = backend.data(query.offset, min(query.limit, size - query.offset))
                        backend.downloaded.append((query.location.id, query.offset))
                        backend.events.append(("download", query.offset))
                        return raw.types.upload.File(type=raw.types.storage.FileUnknown(), mtime=0, bytes=data)
                    if backend.gate:
                        await backend.gate.wait()
                    if backend.false_ack:
                        return False
                    backend.events.append(("upload", query.file_part))
                    backend.parts[query.file_id][query.file_part] = query.bytes
                    return True
                finally:
                    backend.active_rpc[role] -= 1

        monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)

    @staticmethod
    def data(offset, size):
        return bytes([(offset // PART_SIZE) % 251]) * size

    def payload(self, size):
        # Each 1 MiB download chunk has its own repeated marker.
        return b"".join(self.data(offset, min(CHUNK_SIZE, size - offset)) for offset in range(0, size, CHUNK_SIZE))


@pytest.mark.parametrize("size", [1, PART_SIZE, CHUNK_SIZE, CHUNK_SIZE + 13, 10*CHUNK_SIZE, 12*CHUNK_SIZE + 17])
async def test_stream_integrity_checksums_and_boundaries(monkeypatch, size):
    backend = Backend(monkeypatch)
    backend.size[1] = size
    source, bot = clients()
    manager = TransferManager()
    progress = AsyncMock()
    try:
        result = await manager.prepare(source, bot, message(size), progress)
        payload = b"".join(value for _, value in sorted(backend.parts[result.file.id].items()))
        assert payload == backend.payload(size)
        assert result.file.parts == (size + PART_SIZE - 1) // PART_SIZE
        if size <= 10*CHUNK_SIZE:
            assert isinstance(result.file, raw.types.InputFile)
            assert result.file.md5_checksum == hashlib.md5(payload).hexdigest()
        else:
            assert isinstance(result.file, raw.types.InputFileBig)
        assert progress.call_args.args[1:] == (size, size, size)
        assert manager.budget.used == 0
    finally:
        await manager.close()
    assert not any(backend.live.values())


async def test_upload_starts_before_download_finishes(monkeypatch):
    backend = Backend(monkeypatch)
    size = 24*CHUNK_SIZE
    backend.size[1] = size
    source, bot = clients()
    manager = TransferManager()
    try:
        await manager.prepare(source, bot, message(size))
        first_upload = next(i for i, event in enumerate(backend.events) if event[0] == "upload")
        last_download = max(i for i, event in enumerate(backend.events) if event[0] == "download")
        assert first_upload < last_download
    finally:
        await manager.close()


async def test_acknowledgements_are_required(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = CHUNK_SIZE
    backend.false_ack = True
    source, bot = clients()
    manager = TransferManager()
    try:
        async def no_retry(operation):
            return await operation()
        monkeypatch.setattr(transfer, "retry", no_retry)
        with pytest.raises(OSError, match="acknowledge"):
            await manager.prepare(source, bot, message(CHUNK_SIZE))
        assert manager.budget.used == 0
        assert not any(backend.active_rpc.values())
    finally:
        await manager.close()


async def test_global_limits_slow_upload_and_cancel(monkeypatch):
    backend = Backend(monkeypatch)
    backend.gate = asyncio.Event()
    source, bot = clients()
    manager = TransferManager(downloads=4, uploads=4, active=2, buffer_mib=16)
    for index in range(1, 7):
        backend.size[index] = 100*CHUNK_SIZE
    abort = asyncio.Event()
    tasks = [asyncio.create_task(manager.prepare(source, bot, message(100*CHUNK_SIZE, i), abort_event=abort))
             for i in range(1, 7)]
    try:
        await asyncio.sleep(0.1)
        assert manager.budget.used <= 16*CHUNK_SIZE
        assert {i for i, _ in backend.downloaded} <= {1, 2}
        assert backend.peak["source"] <= 4 and backend.peak["bot"] <= 4
        assert backend.rpc_peak["source"] <= manager.downloads.capacity
        assert backend.rpc_peak["bot"] <= manager.uploads.capacity
        abort.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert manager.budget.used == 0
        assert not any(backend.active_rpc.values())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await manager.close()


@pytest.mark.parametrize("error", [FloodWait(31), FloodPremiumWait(1), AuthKeyDuplicated(), ValueError("bad chunk")])
async def test_fatal_errors_propagate_and_drain(monkeypatch, error):
    backend = Backend(monkeypatch)
    backend.error = error
    backend.size[1] = 12*CHUNK_SIZE
    source, bot = clients()
    manager = TransferManager()
    try:
        with pytest.raises(type(error)):
            await manager.prepare(source, bot, message(12*CHUNK_SIZE))
        assert manager.budget.used == 0
        assert not any(backend.active_rpc.values())
    finally:
        await manager.close()


@pytest.mark.parametrize("direction", ["download", "upload"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_premium_wait_keeps_file_and_acknowledged_parts(monkeypatch, direction, cancel):
    """Use real pinned Session.invoke to prove only the refused RPC is repeated."""
    from pyrogram.session import Session
    backend = Backend(monkeypatch)
    backend.size[1] = 12 * CHUNK_SIZE + 13
    original = transfer.MTProtoWorkerSession
    refused = False
    attempts = defaultdict(int)
    waits = []
    waiting = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        if seconds == 9:
            waits.append(seconds)
            if cancel:
                waiting.set()
                await asyncio.Event().wait()
            await real_sleep(0)
        else:
            await real_sleep(seconds)

    class Worker(original):
        WAIT_TIMEOUT = 2
        def __init__(self, client, dc):
            super().__init__(client, dc)
            client.name = client.role
            self.is_started = asyncio.Event()
            self.is_started.set()

        async def invoke(self, query, **kwargs):
            assert kwargs["sleep_threshold"] == 30
            return await Session.invoke(self, query, **kwargs)

        async def send(self, query, timeout):
            nonlocal refused
            download = isinstance(query, raw.functions.upload.GetFile)
            key = ("download", query.offset) if download else ("upload", query.file_part)
            attempts[key] += 1
            target = ("download", CHUNK_SIZE) if direction == "download" else ("upload", 1)
            if key == target and not refused:
                refused = True
                raise FloodPremiumWait(9)
            return await super().invoke(query)

    monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    source, bot = clients()
    manager = TransferManager()
    abort = asyncio.Event()
    task = asyncio.create_task(manager.prepare(source, bot, message(backend.size[1]), abort_event=abort))
    try:
        if cancel:
            await asyncio.wait_for(waiting.wait(), 2)
            abort.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert manager.budget.used == 0
            assert not any(backend.active_rpc.values())
            return
        uploaded = await task
        assert waits == [9]
        assert attempts[("download", 0)] == attempts[("upload", 0)] == 1
        target = ("download", CHUNK_SIZE) if direction == "download" else ("upload", 1)
        assert attempts[target] == 2
        assert len(backend.parts) == 1, "the same upload handle must survive the wait"
        assert b"".join(data for _, data in sorted(backend.parts[uploaded.file.id].items())) == backend.payload(backend.size[1])
        assert manager.budget.used == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


def test_production_restores_single_request_per_connection():
    assert transfer.PyroConf.DOWNLOAD_REQUESTS_PER_CONNECTION == 1
    assert transfer.PyroConf.UPLOAD_REQUESTS_PER_CONNECTION == 1


async def test_reference_refresh_once(monkeypatch):
    backend = Backend(monkeypatch)
    backend.error = FileReferenceExpired()
    backend.size[1] = 12*CHUNK_SIZE + 3
    source, bot = clients()
    source.get_messages = AsyncMock(return_value=message(backend.size[1]))
    manager = TransferManager()
    try:
        result = await manager.prepare(source, bot, message(backend.size[1]))
        source.get_messages.assert_awaited_once()
        assert sum(map(len, backend.parts[result.file.id].values())) == backend.size[1]
        assert manager.budget.used == 0
    finally:
        await manager.close()


@pytest.mark.parametrize("truncated", [False, True])
async def test_cdn_uses_native_streaming_and_checks_size(monkeypatch, truncated):
    backend = Backend(monkeypatch)
    backend.cdn = True
    backend.size[1] = 12*CHUNK_SIZE + 9
    source, bot = clients()
    closed = []
    async def stream(encoded):
        try:
            for offset in range(0, backend.size[1], CHUNK_SIZE):
                if truncated and offset > 0:
                    return
                yield backend.data(offset, min(CHUNK_SIZE, backend.size[1] - offset))
        finally:
            closed.append(True)
    source.stream_media = stream
    manager = TransferManager()
    try:
        if truncated:
            with pytest.raises(ValueError, match="before"):
                await manager.prepare(source, bot, message(backend.size[1]))
        else:
            result = await manager.prepare(source, bot, message(backend.size[1]))
            assert b"".join(data for _, data in sorted(backend.parts[result.file.id].items())) == backend.payload(backend.size[1])
        assert closed
        assert manager.budget.used == 0
    finally:
        await manager.close()


async def test_failed_session_start_cleans_up_and_releases_slot(monkeypatch):
    source, bot = clients()
    stopped = []
    class Worker:
        def __init__(self, client, dc):
            pass
        async def start(self):
            raise ConnectionError("startup failed")
        async def stop(self):
            stopped.append(True)
    monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)
    pool = transfer.ConnectionPool(4)
    with pytest.raises(ConnectionError):
        async with pool.lease(source, 4):
            pass
    assert stopped and pool.available.qsize() == 4
    assert not any(pool.sessions)


async def test_upload_limit_checked_before_fetch(monkeypatch):
    backend = Backend(monkeypatch)
    source, bot = clients()
    manager = TransferManager()
    with pytest.raises(ValueError, match="upload limit"):
        await manager.prepare(source, bot, message(2100*CHUNK_SIZE))
    assert not backend.events and manager.budget.used == 0


async def test_missing_part_repair_and_stable_random_id(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = 12*CHUNK_SIZE + 19
    source, bot = clients()
    manager = TransferManager()
    try:
        upload = await manager.prepare(source, bot, message(backend.size[1]))
        del backend.parts[upload.file.id][1]
        bot.invoke = AsyncMock(side_effect=[FilePartMissing(1), NS(updates=[])])
        query = raw.functions.messages.SendMedia(peer=raw.types.InputPeerSelf(), media=raw.types.InputMediaUploadedDocument(
            file=upload.file, mime_type=upload.mime, attributes=[]), message="caption", random_id=1234)
        await transfer.finalize(bot, query, [upload], manager)
        assert backend.parts[upload.file.id][1] == backend.data(0, CHUNK_SIZE)[PART_SIZE:]
        assert all(call.args[0] is query for call in bot.invoke.await_args_list)
        assert manager.budget.used == 0
    finally:
        await manager.close()


async def test_album_order_metadata_and_parallel_preparation(monkeypatch):
    source, bot = clients()
    manager = TransferManager()
    monkeypatch.setattr(transfer, "get_manager", lambda: manager)
    messages = [message(CHUNK_SIZE, i, "video") for i in range(1, 4)]
    for msg in messages:
        msg.video.duration, msg.video.width, msg.video.height = 3, 640, 480
    async def prepare(_source, _bot, msg, progress):
        await asyncio.sleep((4-msg.id)*0.001)
        return Upload(source, msg, "video", CHUNK_SIZE, f"{msg.id}.mp4", "video/mp4",
                      raw.types.InputFile(id=msg.id, parts=2, name=f"{msg.id}.mp4", md5_checksum="x"))
    monkeypatch.setattr(manager, "prepare", prepare)
    async def invoke(query):
        if isinstance(query, raw.functions.messages.UploadMedia):
            assert query.media.attributes[1].w == 640
            return NS(document=NS(id=query.media.file.id, access_hash=99, file_reference=b"ref"))
        assert [item.media.id.id for item in query.multi_media] == [1, 2, 3]
        return NS(updates=[])
    bot.invoke = AsyncMock(side_effect=invoke)
    monkeypatch.setattr(transfer, "sent_messages", AsyncMock(return_value=[NS(id=101)]))
    result = await transfer.relay_album(source, bot, messages, 123)
    assert result[0].id == 101
    assert bot.invoke.await_count == 4


async def test_caption_entities_preserved(monkeypatch):
    source, bot = clients()
    msg = message(CHUNK_SIZE)
    entity = NS(write=AsyncMock(return_value=raw.types.MessageEntityBold(offset=0, length=7)))
    msg.caption_entities = [entity]
    parsed = await transfer.caption(bot, msg)
    assert parsed["message"] == "caption"
    assert isinstance(parsed["entities"][0], raw.types.MessageEntityBold)
    bot.parser.parse.assert_not_awaited()


async def test_disk_fallback_rejects_without_space(monkeypatch):
    source, bot = clients()
    manager = TransferManager()
    msg = message(CHUNK_SIZE)
    msg.document.file_id = "invalid"
    monkeypatch.setattr(transfer.shutil, "disk_usage", lambda path: NS(free=100*CHUNK_SIZE))
    with pytest.raises(ValueError, match="disk space"):
        await manager.prepare(source, bot, msg)
    assert manager.disk_reserved == 0


async def test_progress_reports_acknowledged_bytes_and_throttles(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
    msg = NS(edit=AsyncMock())
    reporter = transfer.TransferProgress(msg)
    await reporter("one", CHUNK_SIZE, 0, 2*CHUNK_SIZE)
    msg.edit.assert_not_awaited()
    clock[0] = 5
    await reporter("one", CHUNK_SIZE, PART_SIZE, 2*CHUNK_SIZE)
    assert "Uploaded (confirmed): 0.5/2.0" in msg.edit.call_args.args[0]
    clock[0] = 6
    await reporter("one", 2*CHUNK_SIZE, CHUNK_SIZE, 2*CHUNK_SIZE)
    assert msg.edit.await_count == 1


async def test_transient_download_and_upload_retries(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = 3*CHUNK_SIZE
    backend.error = TimeoutError("temporary")
    source, bot = clients()
    manager = TransferManager()
    try:
        upload = await manager.prepare(source, bot, message(backend.size[1]))
        assert len(backend.parts[upload.file.id]) == 6
        attempts = 0
        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionResetError("temporary upload error")
            return True
        assert await transfer.retry(operation) is True
        assert attempts == 3
    finally:
        await manager.close()


async def test_pool_reuses_sessions_and_switches_dc_without_growth(monkeypatch):
    backend = Backend(monkeypatch)
    source, _ = clients()
    pool = transfer.ConnectionPool(2)
    for dc in (2, 2, 4, 4, 5, 5, 2):
        async with pool.lease(source, dc):
            pass
    assert backend.peak["source"] <= 2
    assert backend.live["source"] == 2
    await pool.close()
    assert backend.live["source"] == 0


async def test_thumbnail_uses_shared_uploader(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = CHUNK_SIZE
    backend.size[2] = 10000
    source, bot = clients()
    manager = TransferManager()
    msg = message(CHUNK_SIZE, kind="video")
    msg.video.duration, msg.video.width, msg.video.height = 2, 1280, 720
    thumb = message(10000, 2).document
    msg.video.thumbs = [thumb]
    try:
        upload = await manager.prepare(source, bot, msg)
        media = await transfer.uploaded_media(bot, upload, manager)
        assert media.thumb is upload.thumbnail.file
        assert len(backend.parts[media.thumb.id][0]) == 10000
        assert media.attributes[1].supports_streaming
        assert media.attributes[1].duration == 2
        assert backend.peak["source"] <= 4 and backend.peak["bot"] <= 4
        assert manager.budget.used == 0
    finally:
        await manager.close()


async def test_disk_fallback_uploads_and_removes_file(monkeypatch):
    backend = Backend(monkeypatch)
    source, bot = clients()
    manager = TransferManager()
    msg = message(128)
    msg.document.file_id = "invalid"
    paths = []
    async def download(file_name):
        paths.append(file_name)
        with open(file_name, "wb") as handle:
            handle.write(b"F"*128)
        return file_name
    msg.download = download
    try:
        result = await manager.prepare(source, bot, msg)
        assert backend.parts[result.file.id][0] == b"F"*128
        import os
        assert paths and not os.path.exists(paths[0])
        assert manager.disk_reserved == 0 and manager.budget.used == 0
    finally:
        await manager.close()


async def test_album_failure_never_sends_partial_group(monkeypatch):
    source, bot = clients()
    manager = TransferManager()
    monkeypatch.setattr(transfer, "get_manager", lambda: manager)
    async def prepare(*args):
        raise FloodWait(31)
    monkeypatch.setattr(manager, "prepare", prepare)
    bot.invoke = AsyncMock()
    with pytest.raises(FloodWait):
        await transfer.relay_album(source, bot, [message(CHUNK_SIZE, 1), message(CHUNK_SIZE, 2)], 123)
    bot.invoke.assert_not_awaited()


async def test_single_relay_waits_for_sent_message(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = CHUNK_SIZE
    source, bot = clients()
    manager = TransferManager()
    monkeypatch.setattr(transfer, "get_manager", lambda: manager)
    bot.invoke = AsyncMock(return_value=NS(updates=[]))
    try:
        with pytest.raises(RuntimeError, match="sent message"):
            await transfer.relay_media(source, bot, message(CHUNK_SIZE), 123)
        assert isinstance(bot.invoke.call_args.args[0], raw.functions.messages.SendMedia)
        assert manager.budget.used == 0
    finally:
        await manager.close()


def test_pinned_runtime_and_handler_workers():
    import importlib.metadata
    from pyrogram.crypto import aes
    import main
    assert importlib.metadata.version("Pyrofork") == "2.3.69"
    assert aes.tgcrypto is not None
    assert main.bot.kwargs["workers"] == main.user.kwargs["workers"] == 8


async def test_small_global_buffer_cannot_deadlock_ordered_producers(monkeypatch):
    backend = Backend(monkeypatch)
    source, bot = clients()
    manager = TransferManager(downloads=4, uploads=4, active=2, buffer_mib=4)
    for index in (1, 2):
        backend.size[index] = 16*CHUNK_SIZE + 7
    try:
        results = await asyncio.wait_for(asyncio.gather(
            manager.prepare(source, bot, message(backend.size[1], 1)),
            manager.prepare(source, bot, message(backend.size[2], 2)),
        ), 5)
        assert len(results) == 2
        assert manager.budget.used == 0 and manager.budget.peak <= 4*CHUNK_SIZE
    finally:
        await manager.close()


async def test_real_pyrofork_sent_response_parsing():
    """Catch the production topics error with the real TL constructor and parser."""
    _, bot = clients()
    bot.message_cache = {}
    raw_message = raw.types.Message(id=101, peer_id=raw.types.PeerUser(user_id=42),
        from_id=raw.types.PeerUser(user_id=99), date=1700000000, message="delivered", out=True, entities=[])
    response = raw.types.Updates(updates=[raw.types.UpdateNewMessage(
        message=raw_message, pts=1, pts_count=1)], users=[
            raw.types.User(id=42, first_name="Target", access_hash=1, usernames=[], restriction_reason=[]),
            raw.types.User(id=99, first_name="Bot", bot=True, access_hash=2, usernames=[], restriction_reason=[])], chats=[], date=1700000000, seq=1)
    parsed = await transfer.sent_messages(bot, response)
    assert len(parsed) == 1 and parsed[0].id == 101 and parsed[0].text == "delivered"
    assert parsed[0].chat.id == 42


async def test_pipeline_uses_multiple_rpcs_without_extra_connections(monkeypatch):
    backend = Backend(monkeypatch)
    backend.delay = 0.01
    backend.size[1] = 32 * CHUNK_SIZE + 13
    source, bot = clients()
    manager = TransferManager(downloads=4, uploads=4, download_requests=2, upload_requests=4)
    backend.gate = asyncio.Event()
    task = asyncio.create_task(manager.prepare(source, bot, message(backend.size[1])))
    try:
        for _ in range(100):
            if backend.rpc_peak["bot"] == 16:
                break
            await asyncio.sleep(0.005)
        assert 4 < backend.rpc_peak["source"] <= 8
        assert 4 < backend.rpc_peak["bot"] <= 16
        assert backend.peak["source"] == backend.peak["bot"] == 4
        backend.gate.set()
        upload = await asyncio.wait_for(task, 2)
        assert manager.budget.peak <= manager.budget.limit
        assert b"".join(data for _, data in sorted(backend.parts[upload.file.id].items())) == backend.payload(backend.size[1])
    finally:
        backend.gate.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_restored_producer_enqueues_in_source_order(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = 12 * CHUNK_SIZE
    source, bot = clients()
    manager = TransferManager()
    original = manager.fetch
    first_chunk = asyncio.Event()

    async def fetch(source, fid, offset, expected):
        if offset == 0:
            await first_chunk.wait()
        return await original(source, fid, offset, expected)

    monkeypatch.setattr(manager, "fetch", fetch)
    task = asyncio.create_task(manager.prepare(source, bot, message(backend.size[1])))
    try:
        for _ in range(100):
            if any(backend.parts.values()):
                break
            await asyncio.sleep(0.005)
        assert not any(backend.parts.values()), "restored producer waits for the first source chunk"
        assert not task.done()
        first_chunk.set()
        result = await asyncio.wait_for(task, 2)
        assert b"".join(data for _, data in sorted(backend.parts[result.file.id].items())) == backend.payload(backend.size[1])
    finally:
        first_chunk.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_progress_edit_cannot_stall_data_workers(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
    gate = asyncio.Event()
    reporter = transfer.TransferProgress(NS(edit=AsyncMock(side_effect=lambda *a, **kw: None)))
    async def edit(*args, **kwargs):
        await gate.wait()
    reporter.message.edit = edit
    clock[0] = 5
    task = asyncio.create_task(reporter("one", CHUNK_SIZE, PART_SIZE, 2 * CHUNK_SIZE))
    await asyncio.sleep(0)
    try:
        await asyncio.wait_for(reporter("one", 2 * CHUNK_SIZE, CHUNK_SIZE, 2 * CHUNK_SIZE), 0.1)
        assert reporter.states["one"][0] == 2 * CHUNK_SIZE
    finally:
        gate.set()
        await task


async def test_pool_switch_waits_for_all_pipelined_rpcs(monkeypatch):
    backend = Backend(monkeypatch)
    source, _ = clients()
    pool = transfer.ConnectionPool(1, requests=3)
    gate = asyncio.Event()
    entered = asyncio.Event()
    async def hold():
        async with pool.lease(source, 2):
            entered.set()
            await gate.wait()
    task = asyncio.create_task(hold())
    await entered.wait()
    async def switch():
        async with pool.lease(source, 4) as worker:
            assert worker.dc == 4
    switching = asyncio.create_task(switch())
    try:
        await asyncio.sleep(0.01)
        assert not switching.done() and backend.live["source"] == 1
        gate.set()
        await asyncio.wait_for(asyncio.gather(task, switching), 2)
        assert backend.peak["source"] == 1
        assert pool.available.qsize() == 3
    finally:
        gate.set()
        await asyncio.gather(task, switching, return_exceptions=True)
        await pool.close()


async def test_broken_rpc_does_not_close_session_used_by_sibling(monkeypatch):
    backend = Backend(monkeypatch)
    source, _ = clients()
    pool = transfer.ConnectionPool(1, requests=2)
    gate = asyncio.Event()
    entered = asyncio.Event()
    async def sibling():
        async with pool.lease(source, 2):
            entered.set()
            await gate.wait()
            assert backend.live["source"] == 1
    task = asyncio.create_task(sibling())
    await entered.wait()
    try:
        with pytest.raises(ConnectionError):
            async with pool.lease(source, 2):
                raise ConnectionError("failed request")
        assert backend.live["source"] == 1
        gate.set()
        await task
        assert backend.live["source"] == 0
        assert pool.available.qsize() == 2
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()


async def test_foreign_dc_authorization_is_shared_by_media_connections(monkeypatch):
    import helpers.fast_download as download
    source = NS(storage=NS(dc_id=AsyncMock(return_value=2), test_mode=AsyncMock(return_value=False)),
        invoke=AsyncMock(return_value=NS(id=7, bytes=b"authorization")))
    create = AsyncMock(return_value=b"K" * 256)
    monkeypatch.setattr(download, "Auth", MagicMock(return_value=NS(create=create)))
    sessions = []
    def session(*args, **kwargs):
        assert args[2] == b"K" * 256 and kwargs["is_media"] is True
        worker = NS(start=AsyncMock(), stop=AsyncMock(), invoke=AsyncMock(return_value=True))
        sessions.append(worker)
        return worker
    monkeypatch.setattr(download, "Session", session)
    pool = transfer.ConnectionPool(4)
    all_entered = asyncio.Event()
    entered = 0
    async def lease():
        nonlocal entered
        async with pool.lease(source, 4):
            entered += 1
            if entered == 4:
                all_entered.set()
            await all_entered.wait()
    try:
        await asyncio.wait_for(transfer.supervised([lease() for _ in range(4)]), 2)
        create.assert_awaited_once()
        source.invoke.assert_awaited_once()
        assert sum(worker.invoke.await_count for worker in sessions) == 1
        assert len(sessions) == 4
    finally:
        await pool.close()


async def test_album_copy_uses_existing_ids_and_topics_safe_parser(monkeypatch):
    _, bot = clients()
    messages = [message(CHUNK_SIZE, i) for i in (3, 1, 2)]
    bot.get_media_group = AsyncMock(return_value=messages)
    parsed = []
    async def parse(client, response):
        assert response.topics == []
        return [NS(id=sent.id) for sent in response.messages]
    monkeypatch.setattr(transfer.utils, "parse_messages", parse)
    async def invoke(query):
        assert [item.media.id.id for item in query.multi_media] == [1, 2, 3]
        assert all(item.message == "caption" for item in query.multi_media)
        updates = [raw.types.UpdateNewMessage(message=raw.types.MessageEmpty(id=i), pts=1, pts_count=1)
            for i in (103, 101, 102)]
        return NS(updates=updates, users=[], chats=[])
    bot.invoke = AsyncMock(side_effect=invoke)
    parsed = await transfer.copy_album(bot, 123, -100123, 2)
    assert [message.id for message in parsed] == [101, 102, 103]


@pytest.mark.parametrize("connections", [2, 4])
async def test_cdn_and_regular_file_share_small_memory_budget(monkeypatch, connections):
    backend = Backend(monkeypatch)
    source, bot = clients()
    size = 12 * CHUNK_SIZE + 5
    backend.size[1] = backend.size[2] = size
    manager = TransferManager(downloads=connections, uploads=4, active=2, buffer_mib=8)
    fetch = manager.fetch
    async def cdn_or_regular(source, fid, offset, expected):
        if fid.media_id == 1:
            raise transfer.CdnRedirect()
        return await fetch(source, fid, offset, expected)
    monkeypatch.setattr(manager, "fetch", cdn_or_regular)
    async def stream(encoded):
        for offset in range(0, size, CHUNK_SIZE):
            await asyncio.sleep(0.001)
            yield backend.data(offset, min(CHUNK_SIZE, size - offset))
    source.stream_media = stream
    try:
        results = await asyncio.wait_for(transfer.supervised([
            manager.prepare(source, bot, message(size, 1)),
            manager.prepare(source, bot, message(size, 2))]), 3)
        for upload in results:
            assert b"".join(data for _, data in sorted(backend.parts[upload.file.id].items())) == backend.payload(size)
        assert manager.budget.used == 0 and manager.budget.peak <= 8 * CHUNK_SIZE
    finally:
        await manager.close()
