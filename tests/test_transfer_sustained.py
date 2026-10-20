"""Sustained load regressions; fake Telegram RPCs, real scheduler and byte pipeline."""
import asyncio
from collections import defaultdict

import pytest
from pyrogram import raw

import helpers.transfer as transfer
from test_transfer import Backend, CHUNK_SIZE, clients, message


async def test_busy_cpu_after_40_percent_does_not_collapse_either_direction(monkeypatch):
    backend = Backend(monkeypatch)
    backend.delay = 0.004
    size = backend.size[1] = 96 * CHUNK_SIZE
    source, bot = clients()
    manager = transfer.TransferManager(downloads=4, uploads=4, buffer_mib=32,
        download_requests=2, upload_requests=2, adaptive=True)
    windows = (manager.download_window, manager.upload_window)
    samples = defaultdict(list)
    busy = lambda: len(backend.downloaded) * CHUNK_SIZE >= 0.4 * size
    for direction, window in zip(("download", "upload"), windows):
        # Accelerate only the control clock so a sustained CPU plateau can be
        # exercised without a minutes-long network test.
        window.interval = 0.004
        window.headroom = lambda: not busy()
        window.memory_pressure = lambda: False
        original = window.sample
        def record(rate, saturated, now, original=original, direction=direction, window=window):
            result = original(rate, saturated, now)
            if busy():
                samples[direction].append(window.limit)
            return result
        monkeypatch.setattr(window, "sample", record)
    try:
        result = await asyncio.wait_for(manager.prepare(source, bot, message(size)), 5)
        for direction in ("download", "upload"):
            assert len(samples[direction]) >= 3
            assert min(samples[direction]) >= 8  # old controller fell to one RPC
        parts = backend.parts[result.file.id]
        assert b"".join(data for _, data in sorted(parts.items())) == backend.payload(size)
        assert len(backend.downloaded) == 96
        assert manager.budget.used == 0
        assert manager.budget.peak <= 32 * CHUNK_SIZE
    finally:
        await manager.close()


async def test_download_window_grows_past_initial_and_reuses_confirmation(monkeypatch):
    backend = Backend(monkeypatch)
    backend.size[1] = backend.size[2] = 16 * CHUNK_SIZE
    source, bot = clients()
    manager = transfer.TransferManager(downloads=4, uploads=4, download_requests=2,
        upload_requests=2, adaptive=True)
    monkeypatch.setattr(manager.resources, "has_headroom", lambda: True)
    monkeypatch.setattr(manager.resources, "memory_pressure", lambda: False)
    for window in (manager.download_window, manager.upload_window):
        window.headroom = lambda: True
        window.memory_pressure = lambda: False
    starts = []
    fetch = manager.fetch
    async def confirm(source, fid, offset, expected):
        if offset == 0:
            starts.append(manager.download_window.limit)
            if fid.media_id == 1:
                control = manager.download_window
                control.next_trial = 0
                assert control.sample(100, True, 5) is None
                assert control.sample(100, True, 10) == "trial"
                assert control.limit == 12 > manager.download_initial
                control.sample(125, True, 15)
                assert control.sample(125, True, 20) == "confirmed"
        return await fetch(source, fid, offset, expected)
    monkeypatch.setattr(manager, "fetch", confirm)
    try:
        for identity in (1, 2):
            await manager.prepare(source, bot, message(backend.size[identity], identity))
        assert starts == [8, 12]
        assert manager.download_profiles
        assert backend.rpc_peak["source"] > manager.download_initial
        assert manager.budget.used == manager.download_window.pending == 0
    finally:
        await manager.close()


async def test_download_throttle_clears_warm_state_in_both_directions(monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from pyrogram.errors import FloodPremiumWait
    manager = transfer.TransferManager()
    manager.profiles["route"] = (60, float("inf"))
    manager.download_profiles["route"] = (60, float("inf"))
    worker = NS(session=NS(invoke=AsyncMock(side_effect=[FloodPremiumWait(1), True])))
    async def sleep(seconds):
        manager.paused_until["download"] = 0
    monkeypatch.setattr(asyncio, "sleep", sleep)
    initial = manager.upload_window.limit
    try:
        await manager.invoke(worker, raw.functions.help.GetConfig(), "download")
        assert not manager.profiles and not manager.download_profiles
        assert manager.upload_window.limit == initial
    finally:
        await manager.close()


@pytest.mark.parametrize("adaptive", [False, True])
async def test_high_throughput_profile_respects_shared_bounds_across_files(monkeypatch, adaptive):
    backend = Backend(monkeypatch)
    source, bot = clients()
    manager = transfer.TransferManager(downloads=12, uploads=12, buffer_mib=64,
        download_requests=4, upload_requests=4, adaptive=adaptive)
    monkeypatch.setattr(manager.resources, "has_headroom", lambda: True)
    size = backend.size[1] = backend.size[2] = 48 * CHUNK_SIZE + 17
    try:
        results = await asyncio.wait_for(transfer.supervised([
            manager.prepare(source, bot, message(size, identity)) for identity in (1, 2)]), 5)
        assert len(results) == 2
        for result in results:
            assert b"".join(data for _, data in sorted(backend.parts[result.file.id].items())) == backend.payload(size)
        assert backend.peak["source"] <= 12 and backend.peak["bot"] <= 12
        assert backend.rpc_peak["source"] <= manager.download_window.maximum
        assert backend.rpc_peak["bot"] <= manager.upload_window.maximum
        assert manager.budget.peak <= 64 * CHUNK_SIZE
        assert manager.budget.used == manager.upload_window.pending == manager.download_window.pending == 0
    finally:
        await manager.close()
