import asyncio
import math
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import raw
from pyrogram.session import Session

import helpers.transfer as transfer
from test_transfer import Backend, CHUNK_SIZE, clients, message
import test_pin_feature as fake


@pytest.mark.parametrize("direction", ["download", "upload"])
async def test_real_session_gets_extended_deadline_and_retries_only_failed_chunk(monkeypatch, direction):
    backend = Backend(monkeypatch)
    original = transfer.MTProtoWorkerSession
    attempts = defaultdict(int)
    monkeypatch.setattr(transfer.PyroConf, "TRANSFER_RPC_TIMEOUT", 60)
    target = (direction, 0)

    class Worker(original):
        WAIT_TIMEOUT = 2
        def __init__(self, client, dc):
            super().__init__(client, dc)
            client.name = client.role
            self.is_started = asyncio.Event()
            self.is_started.set()

        async def invoke(self, query, **kwargs):
            return await Session.invoke(self, query, **kwargs)

        async def send(self, query, timeout):
            assert timeout == 60
            key = (("download", query.offset) if isinstance(query, raw.functions.upload.GetFile)
                   else ("upload", query.file_part))
            attempts[key] += 1
            if key == target and attempts[key] == 1:
                raise TimeoutError("Request timed out")
            return await super().invoke(query)

    monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)
    source, bot = clients()
    backend.size[1] = 12 * CHUNK_SIZE + 17
    manager = transfer.TransferManager()
    try:
        upload = await manager.prepare(source, bot, message(backend.size[1]))
        assert attempts[target] == 2
        assert all(count == 1 for key, count in attempts.items() if key != target)
        assert len(backend.parts) == 1
        assert b"".join(data for _, data in sorted(backend.parts[upload.file.id].items())) == backend.payload(upload.size)
        assert manager.budget.used == 0
    finally:
        await manager.close()


async def test_final_send_timeout_retains_upload_and_random_id(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = 12 * CHUNK_SIZE + 17
    source, bot = clients()
    manager = transfer.TransferManager()
    monkeypatch.setattr(transfer, "get_manager", lambda: manager)
    monkeypatch.setattr(transfer.PyroConf, "TRANSFER_FINALIZE_TIMEOUT", 73)
    monkeypatch.setattr(transfer, "sent_messages", AsyncMock(return_value=[NS(id=501)]))
    calls = []
    async def publish(query, **kwargs):
        assert kwargs == {"timeout": 73, "retries": 0}
        assert sum(map(len, backend.parts[query.media.file.id].values())) == backend.size[1]
        calls.append(query)
        if len(calls) == 1:
            raise TimeoutError("Request timed out")
        return NS(updates=[])
    bot.invoke = publish
    progress = NS(edit=AsyncMock(side_effect=TimeoutError("status edit timed out")))
    try:
        result = await transfer.relay_media(source, bot, message(backend.size[1]), 123, progress_message=progress)
        assert result.id == 501
        assert len(calls) == 2 and calls[0] is calls[1]
        assert calls[0].random_id == calls[1].random_id
        assert len(backend.parts) == 1
        assert len(backend.downloaded) == math.ceil(backend.size[1] / CHUNK_SIZE)
        assert manager.budget.used == 0
    finally:
        await manager.close()


async def test_cancel_while_waiting_for_final_send_drains_request(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = CHUNK_SIZE
    source, bot = clients()
    manager = transfer.TransferManager()
    monkeypatch.setattr(transfer, "get_manager", lambda: manager)
    waiting, stopped, abort = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def publish(query, **kwargs):
        waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    bot.invoke = publish
    task = asyncio.create_task(transfer.relay_media(source, bot, message(CHUNK_SIZE), 123, abort_event=abort))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        abort.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stopped.is_set() and manager.budget.used == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()


async def test_persistent_final_timeout_is_bounded():
    client = NS(invoke=AsyncMock(side_effect=TimeoutError("Request timed out")))
    query = raw.functions.help.GetConfig()
    with pytest.raises(TimeoutError, match="Telegram GetConfig timed out after 3 attempts"):
        await transfer.final_request(client, query)
    assert client.invoke.await_count == 3
    assert all(call.args[0] is query and call.kwargs["retries"] == 0
               for call in client.invoke.await_args_list)


async def test_progress_cleanup_timeout_cannot_turn_sent_video_into_failure(monkeypatch):
    import main
    fake.reset(main.bot, main.user)
    fake.PROTECTED_SOURCE[0] = True
    monkeypatch.setattr(fake.OutgoingMessage, "delete", AsyncMock(side_effect=TimeoutError("Request timed out")))
    try:
        result = await main.handle_download(main.bot, fake.IncomingMessage("link"), "https://t.me/testchan/100")
        assert result["status"] == "success" and result["sent_msg_id"]
        assert not fake.bot_said("Error at")
    finally:
        await fake.drain_tasks()


async def test_progress_never_rounds_incomplete_upload_to_100_percent(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(transfer.time, "monotonic", lambda: clock[0])
    status = NS(edit=AsyncMock())
    progress = transfer.TransferProgress(status)
    progress.begin()
    clock[0] = 5
    await progress("video", 1000, 999, 1000)
    assert "`99%`" in status.edit.call_args.args[0]
    assert "`100%`" not in status.edit.call_args.args[0]
