import asyncio
import socket
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

import helpers.speedtest as speedtest


async def test_speedtest_measures_both_directions_without_disk(monkeypatch):
    uploads = []
    async def down(request):
        return web.Response(body=b"D" * int(request.query["bytes"]))
    async def up(request):
        uploads.append(await request.read())
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/__down", down)
    app.router.add_post("/__up", up)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    monkeypatch.setattr(speedtest, "BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(speedtest, "DOWNLOAD_BYTES", 1024)
    monkeypatch.setattr(speedtest, "UPLOAD_BYTES", 768)
    try:
        result = await speedtest.run_speedtest()
        assert result.download_bytes == 4096 and result.upload_bytes == 3072
        assert len(uploads) == 4 and all(len(body) == 768 for body in uploads)
        assert result.download_seconds > 0 and result.upload_seconds > 0
        text = speedtest.format_speedtest(result)
        assert "MB/s" in text and "Mbps" in text and "Telegram" in text
    finally:
        await runner.cleanup()


async def test_speedtest_rejects_truncated_samples():
    class Content:
        async def iter_chunked(self, size):
            yield b"short"
    class Response:
        content = Content()
        def raise_for_status(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    class Session:
        def get(self, *args, **kwargs):
            return Response()
    with pytest.raises(ValueError, match="truncated"):
        await speedtest.download_sample(Session(), 100)


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_measure_drains_http_workers(cancel):
    entered = asyncio.Event()
    stopped = []
    calls = 0
    async def operation(session, size):
        nonlocal calls
        calls += 1
        identity = calls
        try:
            if identity == 1:
                await entered.wait()
                if not cancel:
                    raise aiohttp.ClientConnectionError("test endpoint failed")
            else:
                entered.set()
            await asyncio.Event().wait()
        finally:
            stopped.append(identity)
    task = asyncio.create_task(speedtest.measure(None, operation, 100))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else aiohttp.ClientConnectionError):
        await task
    assert len(stopped) == speedtest.CONNECTIONS


async def test_speedtest_command_reports_busy_cooldown_and_results(monkeypatch):
    import main
    from test_pin_feature import IncomingMessage
    monkeypatch.setattr(main, "SPEEDTEST_RUNNING", False)
    monkeypatch.setattr(main, "SPEEDTEST_LAST_RUN", 0)
    monkeypatch.setattr(main, "ACTIVE_BATCHES", {1: {}})
    message = IncomingMessage("/speedtest")
    await main.speedtest_command(main.bot, message)
    assert "Transfers are active" in message.replies[-1].text
    monkeypatch.setattr(main, "ACTIVE_BATCHES", {})
    monkeypatch.setattr(main, "RUNNING_TASKS", set())
    result = speedtest.SpeedResult(15, 64 * 1048576, 8, 32 * 1048576, 4)
    run = AsyncMock(return_value=result)
    monkeypatch.setattr(main, "run_speedtest", run)
    await main.speedtest_command(main.bot, message)
    assert "SERVER SPEED TEST" in message.replies[-1].text
    assert "8.39 MB/s" in message.replies[-1].text
    assert main.SPEEDTEST_RUNNING is False
    await main.speedtest_command(main.bot, message)
    assert "cooldown" in message.replies[-1].text
    run.assert_awaited_once()
