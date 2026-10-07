"""Opt-in real transfers. Run only while the production bot is stopped."""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def benchmark(args):
    from pyrogram import Client
    from pyrogram.crypto import aes
    from config import PyroConf
    from helpers.fast_download import extract_media_info
    from helpers.msg import getChatMsgID
    from helpers.utils import send_media
    import helpers.transfer as transfer

    if aes.tgcrypto is None:
        raise RuntimeError("Install TgCrypto before benchmarking")
    target = int(args.target) if args.target.lstrip("-").isdigit() else args.target
    source = Client("benchmark-source", session_string=PyroConf.SESSION_STRING,
                    api_id=PyroConf.API_ID, api_hash=PyroConf.API_HASH, workers=8)
    bot = Client("benchmark-bot", in_memory=True, bot_token=PyroConf.BOT_TOKEN,
                 api_id=PyroConf.API_ID, api_hash=PyroConf.API_HASH, workers=8)
    requester = type("Requester", (), {"chat": type("Chat", (), {"id": target})()})()
    async with source, bot:
        chat, identity, _ = getChatMsgID(args.source)
        message = await source.get_messages(chat, identity)
        kind, _, _, size, name, _ = extract_media_info(message)
        if kind not in ("document", "video", "audio", "photo"):
            raise ValueError("Benchmark a document, video, audio file, or photo")
        transfer.TransferManager().check_size(bot, size)
        reserve = PyroConf.DISK_RESERVE_MIB * 1048576
        if size + reserve <= shutil.disk_usage(tempfile.gettempdir()).free:
            with tempfile.TemporaryDirectory(prefix="restrdl-benchmark-") as directory:
                started = time.monotonic()
                path = await message.download(file_name=os.path.join(directory, transfer.safe_name(name, "baseline.bin")))
                if not path or os.path.getsize(path) != size:
                    raise RuntimeError("Baseline download was incomplete")
                sent = await send_media(bot, requester, path, kind, message.caption,
                                        None, started, destination_chat_id=target)
                if not sent:
                    raise RuntimeError("Baseline upload failed")
                report("native", size, started, sent.id)
        else:
            print(json.dumps({"mode": "native", "skipped": "not enough disk space for baseline"}))
        modes = [("stream-4-before", 4, 1, 1)] + [
            (f"pipeline-{connections}", connections, 2, 4) for connections in args.connections]
        for mode, connections, download_requests, upload_requests in modes:
            manager = transfer.TransferManager(downloads=connections, uploads=connections,
                download_requests=download_requests, upload_requests=upload_requests)
            transfer._manager = manager
            transfer._manager_loop = asyncio.get_running_loop()
            try:
                message = await source.get_messages(chat, identity)
                started = time.monotonic()
                sent = await transfer.relay_media(source, bot, message, target)
                report(mode, size, started, sent.id, manager.budget.peak)
            finally:
                await manager.close()


def report(mode, size, started, message_id, buffer_peak=None):
    elapsed = time.monotonic() - started
    print(json.dumps({"mode": mode, "bytes": size, "seconds": round(elapsed, 3),
                      "MiB_per_second": round(size / 1048576 / elapsed, 3),
                      "MB_per_second": round(size / 1e6 / elapsed, 3),
                      "sent_message_id": message_id, "buffer_peak_bytes": buffer_peak}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Actually upload test posts to the destination")
    parser.add_argument("--connections", nargs="+", type=int, choices=range(2, 9), default=[2, 4],
                        help="Connection counts to test for the new pipeline (default: 2 4)")
    parser.add_argument("--source", required=True, help="Representative Telegram post URL")
    parser.add_argument("--target", required=True, help="Destination chat ID or username")
    args = parser.parse_args()
    if not args.run:
        parser.error("Add --run to perform real downloads/uploads. Stop the production bot first.")
    asyncio.run(benchmark(args))
