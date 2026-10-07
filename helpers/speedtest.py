"""Bounded, asynchronous server bandwidth sample against Cloudflare's edge."""

import asyncio
import os
import shutil
import statistics
import time
from dataclasses import dataclass

import aiohttp

BASE_URL = "https://speed.cloudflare.com"
CONNECTIONS = 4
# Cloudflare's public endpoint rejects the old 16 MiB per-request sample in
# some hosting regions. These four parallel samples keep the test meaningful
# while staying inside its public endpoint limits.
DOWNLOAD_BYTES = 8 * 1048576
UPLOAD_BYTES = 4 * 1048576
BLOCK_SIZE = 64 * 1024
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/131.0.0.0 Safari/537.36"
)
CURL = shutil.which("curl")


class SpeedtestError(RuntimeError):
    """Retains an actionable phase when the bot reports a failed test."""
    def __init__(self, phase, error):
        self.phase = phase
        self.error = error
        super().__init__(f"{phase}: {error}")


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
    payload = "0" * size
    async with session.post(f"{BASE_URL}/__up", params={"bytes": size}, data=payload,
                            headers={"Content-Type": "text/plain;charset=UTF-8"}) as response:
        response.raise_for_status()
        received = 0
        async for chunk in response.content.iter_chunked(BLOCK_SIZE):
            received += len(chunk)
            if received > BLOCK_SIZE:
                raise ValueError("Unexpected speed test upload response")
    return size


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


async def curl_sample(direction, size):
    """Use curl's TLS transport for Cloudflare hosts that reject aiohttp."""
    endpoint = "down" if direction == "download" else "up"
    url = f"{BASE_URL}/__{endpoint}?bytes={size}&nonce={os.urandom(8).hex()}"
    command = [
        CURL, "--silent", "--show-error", "--fail", "--location",
        "--connect-timeout", "8", "--max-time", "30", "--user-agent", USER_AGENT,
        "--output", os.devnull, "--write-out", "%{size_download} %{time_total} %{http_code}",
    ]
    if direction == "upload":
        command.extend([
            "--request", "POST", "--header", "Content-Type: text/plain;charset=UTF-8",
            "--data-binary", "@-",
        ])
    command.append(url)
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if direction == "upload" else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        if direction == "upload":
            remaining = size
            block = b"0" * BLOCK_SIZE
            while remaining:
                chunk = block[:min(BLOCK_SIZE, remaining)]
                process.stdin.write(chunk)
                await process.stdin.drain()
                remaining -= len(chunk)
            process.stdin.close()
        stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        message = stderr.decode("utf-8", "replace").strip() or f"curl exited {process.returncode}"
        raise RuntimeError(message[:300])
    try:
        downloaded, elapsed, status = stdout.decode().strip().split()
        transferred = int(downloaded) if direction == "download" else size
        if int(status) != 200:
            raise ValueError(f"HTTP {status}")
        if direction == "download" and transferred != size:
            raise ValueError(f"expected {size} downloaded bytes, received {transferred}")
        return transferred, max(float(elapsed), 0.001)
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"invalid curl measurement: {stdout!r}") from error


async def curl_measure(direction, size):
    tasks = [asyncio.create_task(curl_sample(direction, size)) for _ in range(CONNECTIONS)]
    started = time.monotonic()
    try:
        results = await asyncio.gather(*tasks)
        return sum(count for count, _ in results), max(time.monotonic() - started, 0.001)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def curl_latency():
    samples = [await curl_sample("download", 0) for _ in range(3)]
    return statistics.median(elapsed * 1000 for _, elapsed in samples)


async def _run_speedtest():
    if CURL and BASE_URL == "https://speed.cloudflare.com":
        try:
            latency = await curl_latency()
            down, down_time = await curl_measure("download", DOWNLOAD_BYTES)
            up, up_time = await curl_measure("upload", UPLOAD_BYTES)
            return SpeedResult(latency, down, down_time, up, up_time)
        except Exception as error:
            raise SpeedtestError("Cloudflare curl test", error) from error

    timeout = aiohttp.ClientTimeout(total=25, connect=8)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
        headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache", "User-Agent": USER_AGENT},
        connector=aiohttp.TCPConnector(limit=CONNECTIONS)) as session:
        try:
            latencies = []
            for _ in range(3):
                started = time.monotonic()
                await download_sample(session, 0)
                latencies.append((time.monotonic() - started) * 1000)
        except Exception as error:
            raise SpeedtestError("latency", error) from error
        try:
            down, down_time = await measure(session, download_sample, DOWNLOAD_BYTES)
        except Exception as error:
            raise SpeedtestError("download", error) from error
        try:
            up, up_time = await measure(session, upload_sample, UPLOAD_BYTES)
        except Exception as error:
            raise SpeedtestError("upload", error) from error
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


def format_speedtest_error(error):
    phase = getattr(error, "phase", "connection")
    cause = getattr(error, "error", error)
    if isinstance(cause, aiohttp.ClientResponseError):
        detail = f"HTTP `{cause.status}` from Cloudflare during {phase}."
    elif isinstance(cause, asyncio.TimeoutError):
        detail = f"Timed out during {phase}."
    else:
        detail = f"{type(cause).__name__} during {phase}: `{str(cause)[:180] or 'no detail'}`"
    return (
        "⚠️ **SERVER SPEED TEST FAILED**\n\n"
        f"{detail}\n\n"
        "The bot is still running. Try again later or use `/logs` for the full server-side error."
    )
