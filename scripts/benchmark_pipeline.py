"""Offline scheduler benchmark. No Telegram sessions, credentials or posts are used."""
import argparse
import asyncio
import json
import math
import os
import sys
import time
from types import SimpleNamespace

# The transfer module imports the service config; placeholders are local to this process.
os.environ.setdefault("BOT_TOKEN", "123456:OFFLINE")
os.environ.setdefault("SESSION_STRING", "offline-benchmark")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.transfer import CHUNK_SIZE, PART_SIZE, TransferManager


class SimulatedTransfer(TransferManager):
    def __init__(self, download_requests, upload_requests, latency):
        super().__init__(downloads=4, uploads=4, active=1, buffer_mib=64,
                         download_requests=download_requests, upload_requests=upload_requests,
                         adaptive=False)
        self.read_slots = asyncio.Semaphore(self.downloads.capacity)
        self.write_slots = asyncio.Semaphore(self.uploads.capacity)
        self.latency = latency
        self.parts = set()
        self.acked = 0
        self.first_ack = None

    async def fetch(self, source, fid, offset, expected):
        async with self.read_slots:
            await asyncio.sleep(self.latency)
            return bytes([(offset // CHUNK_SIZE) % 251]) * expected

    async def put_part(self, bot, file_id, part, count, size, data):
        async with self.write_slots:
            await asyncio.sleep(self.latency)
            expected = min(PART_SIZE, size - part * PART_SIZE)
            marker = ((part * PART_SIZE) // CHUNK_SIZE) % 251
            if len(data) != expected or data != bytes([marker]) * expected or part in self.parts:
                raise ValueError("Synthetic payload corruption or duplicate part")
            self.parts.add(part)
            self.acked += len(data)
            if self.first_ack is None:
                self.first_ack = time.perf_counter()


async def benchmark(args):
    size = args.mib * CHUNK_SIZE + 17
    for mode, down, up in (("one-request", 1, 1), ("pipelined", 2, 4)):
        for repeat in range(args.repeats):
            manager = SimulatedTransfer(down, up, args.latency_ms / 1000)
            bot = SimpleNamespace(rnd_id=lambda: 12345)
            message = SimpleNamespace(id=1, chat=SimpleNamespace(id=1))
            started = time.perf_counter()
            cpu_started = time.process_time()
            try:
                await manager.pipe(None, bot, message, None, size, "synthetic.bin", None)
                elapsed = time.perf_counter() - started
                if manager.acked != size or len(manager.parts) != math.ceil(size / PART_SIZE):
                    raise ValueError("Incomplete synthetic upload")
                print(json.dumps({
                    "mode": mode, "repeat": repeat + 1, "bytes": size,
                    "seconds": round(elapsed, 4),
                    "ack_MiB_per_second": round(size / CHUNK_SIZE / elapsed, 3),
                    "first_ack_seconds": round(manager.first_ack - started, 4),
                    "cpu_seconds": round(time.process_time() - cpu_started, 4),
                    "buffer_peak_bytes": manager.budget.peak,
                    "download_RPC_limit": manager.downloads.capacity,
                    "upload_RPC_limit": manager.uploads.capacity,
                }))
            finally:
                await manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, choices=range(1, 513), default=64)
    parser.add_argument("--latency-ms", type=float, default=20)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    if not 0 < args.latency_ms <= 1000:
        parser.error("--latency-ms must be in (0, 1000]")
    asyncio.run(benchmark(args))
