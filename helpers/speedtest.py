"""Bounded, asynchronous server bandwidth sample against Cloudflare's edge."""

import asyncio
import os
import statistics
import time
from dataclasses import dataclass

import aiohttp

BASE_URL = "https://speed.cloudflare.com"
CONNECTIONS = 4
DOWNLOAD_BYTES = 16 * 1048576
UPLOAD_BYTES = 8 * 1048576
BLOCK_SIZE = 64 * 1024


@dataclass
class SpeedResult:
    latency_ms: float
    download_bytes: int
    download_seconds: float
    upload_bytes: int
    upload_seconds: float


async def download_sample(session, size):
    count = 0
    async with session.get(f"{BASE_URL}/__down", params={"bytes": size, "nonce": os.urandom(8).hex()}) as response:
        response.raise_for_status()
        async for block in response.content.iter_chunked(BLOCK_SIZE):
            count += len(block)
            if count > size:
                raise ValueError("Speed test download exceeded its requested size")
    if count != size:
        raise ValueError("Speed test download was truncated")
    return count


async def upload_sample(session, size):
    sent = 0
    block = os.urandom(BLOCK_SIZE)

    async def payload():
        nonlocal sent
        while sent < size:
            chunk = block[:min(BLOCK_SIZE, size - sent)]
            sent += len(chunk)
            yield chunk

    async with session.post(f"{BASE_URL}/__up", data=payload(), headers={
        "Content-Length": str(size), "Content-Type": "application/octet-stream",
    }) as response:
        response.raise_for_status()
        received = 0
        async for chunk in response.content.iter_chunked(BLOCK_SIZE):
            received += len(chunk)
            if received > BLOCK_SIZE:
                raise ValueError("Unexpected speed test upload response")
    if sent != size:
        raise ValueError("Speed test upload did not send the requested payload")
    return sent


async def measure(session, operation, size):
    tasks = [asyncio.create_task(operation(session, size)) for _ in range(CONNECTIONS)]
    started = time.monotonic()
    try:
        counts = await asyncio.gather(*tasks)
        return sum(counts), max(time.monotonic() - started, 0.001)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_speedtest():
    timeout = aiohttp.ClientTimeout(total=25, connect=8)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
        headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"},
        connector=aiohttp.TCPConnector(limit=CONNECTIONS)) as session:
        latencies = []
        for _ in range(3):
            started = time.monotonic()
            await download_sample(session, 0)
            latencies.append((time.monotonic() - started) * 1000)
        down, down_time = await measure(session, download_sample, DOWNLOAD_BYTES)
        up, up_time = await measure(session, upload_sample, UPLOAD_BYTES)
        return SpeedResult(statistics.median(latencies), down, down_time, up, up_time)


async def run_speedtest():
    # Overall deadline also cancels and drains every HTTP worker.
    return await asyncio.wait_for(_run_speedtest(), timeout=75)


def format_speedtest(result):
    down = result.download_bytes / result.download_seconds
    up = result.upload_bytes / result.upload_seconds
    return (
        "🌐 **SERVER SPEED TEST**\n━━━━━━━━━━━━━━━━━━━\n\n"
        f"📥 **Download** `{down / 1e6:.2f} MB/s` • `{down * 8 / 1e6:.1f} Mbps`\n"
        f"📤 **Upload** `{up / 1e6:.2f} MB/s` • `{up * 8 / 1e6:.1f} Mbps`\n"
        f"📡 **Latency (HTTP)** `{result.latency_ms:.0f} ms`\n\n"
        "🧪 **Test details**\nCloudflare edge • 4 parallel connections\n"
        f"Traffic: `{(result.download_bytes + result.upload_bytes) / 1048576:.0f} MiB`\n"
        "No whole-file RAM or disk buffer\n\n"
        "ℹ️ This measures the server's route to Cloudflare. Telegram's route and account limits "
        "can give different speeds. **7.5 MB/s = 60 Mbps**; Telegram progress uses MiB/s."
    )
