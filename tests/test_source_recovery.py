"""Fault injection for congested source connections and incomplete reads."""
import asyncio
from collections import Counter
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from pyrogram import raw

import helpers.transfer as transfer
from helpers.fast_download import MTProtoWorkerSession
from helpers.transfer_window import UploadWindow
from test_transfer import Backend, CHUNK_SIZE, clients, message


def test_production_source_isolation_applies_over_legacy_request_settings(monkeypatch):
    monkeypatch.setattr(transfer.PyroConf, "DOWNLOAD_ISOLATED_READS", True)
    monkeypatch.setattr(transfer.PyroConf, "DOWNLOAD_REQUESTS_PER_CONNECTION", 4)
    monkeypatch.setattr(transfer.PyroConf, "MAX_DOWNLOAD_REQUESTS_PER_CONNECTION", 8)
    manager = transfer.TransferManager(downloads=12)
    assert manager.isolated_reads
    assert manager.downloads.requests == 1
    assert manager.download_initial == manager.downloads.capacity == 12
    assert manager.download_window.maximum == 12


async def test_stalled_chunk_retries_after_replacing_connection_without_restarting_file(monkeypatch):
    backend = Backend(monkeypatch)
    original = transfer.MTProtoWorkerSession
    backend.delay = 0
    size = backend.size[1] = 32 * CHUNK_SIZE + 17
    source, bot = clients()
    attempts = Counter()
    connections = []
    canceled = asyncio.Event()
    monkeypatch.setattr(transfer.PyroConf, "DOWNLOAD_STALL_TIMEOUT", 0.04)
    monkeypatch.setattr(transfer.PyroConf, "DOWNLOAD_ISOLATED_READS", True)
    class Worker(original):
        async def invoke(self, query, **kwargs):
            if isinstance(query, raw.functions.upload.GetFile):
                attempts[query.offset] += 1
                if query.offset == 0:
                    connections.append(self)
                    if attempts[0] == 1:
                        try:
                            await asyncio.Event().wait()  # readiness/drain stuck, no response timer
                        finally:
                            canceled.set()
            return await super().invoke(query, **kwargs)
    monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)
    manager = transfer.TransferManager(downloads=4, uploads=4, buffer_mib=16)
    upload_calls = Counter()
    put_part = manager.put_part
    async def upload(*args):
        upload_calls[args[2]] += 1
        await put_part(*args)
    monkeypatch.setattr(manager, "put_part", upload)
    try:
        result = await asyncio.wait_for(manager.prepare(source, bot, message(size)), 3)
        assert canceled.is_set()
        assert attempts[0] == 2 and len(attempts) == 33
        assert all(count == 1 for offset, count in attempts.items() if offset != 0)
        assert connections[0] is not connections[1]
        assert not connections[0].started
        assert set(upload_calls.values()) == {1}
        assert b"".join(data for _, data in sorted(backend.parts[result.file.id].items())) == backend.payload(size)
        assert manager.timeout_count["download"] == 1
        assert manager.budget.used == manager.download_window.pending == 0
    finally:
        await manager.close()


async def test_persistent_source_stall_has_bounded_attempts_and_releases_everything(monkeypatch):
    backend = Backend(monkeypatch)
    original = transfer.MTProtoWorkerSession
    attempts = Counter()
    monkeypatch.setattr(transfer.PyroConf, "DOWNLOAD_STALL_TIMEOUT", 0.015)
    monkeypatch.setattr(transfer.PyroConf, "DOWNLOAD_CHUNK_ATTEMPTS", 2)
    class Worker(original):
        async def invoke(self, query, **kwargs):
            if isinstance(query, raw.functions.upload.GetFile):
                attempts[query.offset] += 1
                await asyncio.Event().wait()
            return await super().invoke(query, **kwargs)
    monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)
    source, bot = clients()
    manager = transfer.TransferManager(downloads=4, uploads=4, buffer_mib=16)
    try:
        with pytest.raises(TimeoutError, match="stalled after 2 attempts"):
            await asyncio.wait_for(manager.prepare(source, bot, message(12 * CHUNK_SIZE)), 3)
        assert max(attempts.values()) == 2
        assert manager.budget.used == manager.download_window.active == manager.download_window.pending == 0
        assert manager.downloads.available.qsize() == manager.downloads.capacity
    finally:
        await manager.close()


async def test_each_isolated_source_session_has_only_one_active_rpc_across_files(monkeypatch):
    backend = Backend(monkeypatch)
    original = transfer.MTProtoWorkerSession
    active = Counter()
    peak = Counter()
    class Worker(original):
        async def invoke(self, query, **kwargs):
            if not isinstance(query, raw.functions.upload.GetFile):
                return await super().invoke(query, **kwargs)
            active[id(self)] += 1
            peak[id(self)] = max(peak[id(self)], active[id(self)])
            try:
                return await super().invoke(query, **kwargs)
            finally:
                active[id(self)] -= 1
    monkeypatch.setattr(transfer, "MTProtoWorkerSession", Worker)
    source, bot = clients()
    manager = transfer.TransferManager(downloads=4, uploads=4, buffer_mib=32)
    try:
        for identity in (1, 2):
            backend.size[identity] = 24 * CHUNK_SIZE
        await asyncio.wait_for(transfer.supervised([
            manager.prepare(source, bot, message(backend.size[i], i)) for i in (1, 2)]), 3)
        assert len(peak) == 4 and set(peak.values()) == {1}
        assert not any(active.values())
        assert manager.budget.used == 0
    finally:
        await manager.close()


def test_latency_growth_with_falling_goodput_reduces_source_before_timeout():
    control = UploadWindow(12, 12, 2, interval=3, latency_guard=True)
    control.last_sample = control.next_trial = 0
    control.active = control.pending = 12
    assert control.acknowledge(30 * CHUNK_SIZE, now=3, latency=0.5) is None
    assert control.acknowledge(30 * CHUNK_SIZE, now=6, latency=0.5) is None
    assert control.acknowledge(9 * CHUNK_SIZE, now=9, latency=14) == "source-latency"
    assert control.limit == control.confirmed == 6
    for now in (12, 15, 18):
        control.acknowledge(9 * CHUNK_SIZE, now=now, latency=14)
    assert control.limit == 6  # one episode, not a cut on each late response


def test_cpu_alone_does_not_trigger_source_congestion():
    control = UploadWindow(12, 12, 2, interval=3, latency_guard=True,
                           headroom=lambda: False)
    control.last_sample = 0
    control.active = 12
    for now in range(3, 33, 3):
        control.acknowledge(30 * CHUNK_SIZE, now=now, latency=0.5)
    assert control.limit == 12


async def test_stuck_media_shutdown_aborts_transport_and_drains_tasks():
    worker = MTProtoWorkerSession(NS(), 2)
    ping = asyncio.create_task(asyncio.Event().wait())
    receive = asyncio.create_task(asyncio.Event().wait())
    writer = NS(transport=NS(abort=MagicMock()))
    async def stop():
        await asyncio.Event().wait()
    session = NS(stop=stop, connection=NS(protocol=NS(writer=writer)), ping_task=ping,
                 recv_task=receive, results={123: object()})
    worker.session = session
    await asyncio.wait_for(worker.stop(timeout=0.01), 1)
    assert worker.session is None
    assert ping.done() and receive.done()
    assert not session.results
    writer.transport.abort.assert_called_once()


async def test_cancel_during_source_watchdog_remains_cancellation(monkeypatch):
    entered = asyncio.Event()
    async def hang(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    worker = NS(session=NS(invoke=hang))
    manager = transfer.TransferManager()
    task = asyncio.create_task(manager.invoke(worker, raw.functions.help.GetConfig(), "download"))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.timeout_count["download"] == 0
        assert manager.download_window.active == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()
