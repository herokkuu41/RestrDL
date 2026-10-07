"""Opt-in verification of a real player URL WITHOUT logging in to Telegram.

Run from a disposable working directory with BOT_TOKEN and SESSION_STRING test
placeholders. Supply the lesson password in WATCH_BOARD_TEST_PASSWORD (not CLI
arguments / shell history). Do not commit downloaded media or private passwords.
"""
import argparse
import asyncio
import json
import os
import sys
from time import monotonic
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from helpers.watch_board import resolve_video_cdn_url, download_watch_board_video
from helpers.watch_board_render import load_board_events, render_board_video, _media_metadata


async def run(args):
    os.makedirs(args.output, exist_ok=True)
    camera = await resolve_video_cdn_url(args.url)
    events = await load_board_events(args.url, camera, os.environ.get("WATCH_BOARD_TEST_PASSWORD"))
    source = args.camera or await download_watch_board_video(camera, os.path.join(args.output, "camera.webm"))
    if not source:
        raise RuntimeError("Camera download failed")
    # Diagnostic clips do NOT alter normal full-length batch exports.
    import psutil
    process = psutil.Process()
    peak = 0

    async def memory_monitor():
        nonlocal peak
        while True:
            total = process.memory_info().rss
            for child in process.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            peak = max(peak, total)
            await asyncio.sleep(0.1)

    monitor = asyncio.create_task(memory_monitor())
    start = monotonic()
    try:
        result = await render_board_video(source, events, os.path.join(args.output, "board-face.mp4"),
                                         clip_start=args.start, clip_duration=args.seconds,
                                         layout=args.layout)
    finally:
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
    print(json.dumps({"camera": _media_metadata(source), "output": _media_metadata(result),
                      "event_count": len(events), "render_seconds": round(monotonic() - start, 2),
                      "peak_process_tree_mib": round(peak / 1048576, 1),
                      "output_path": os.path.abspath(result)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", required=True)
    parser.add_argument("--camera", help="Use an already verified local camera track for repeat rendering tests")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--layout", choices=("side_by_side", "pip"), default="side_by_side")
    asyncio.run(run(parser.parse_args()))
