"""Export the site's separate slide/ink track together with its teacher video.

The public WEBM is only the camera track. It is NOT the Watch Board player.
No browser, screen recording, Telegram source download or resolution URL guessing
is used here. Lesson events are obtained through the player's authenticated API.
"""

import asyncio
import io
import json
import math
import os
import re
import shutil
from time import monotonic
from collections import OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image, ImageColor, ImageDraw, ImageOps

from config import PyroConf
from logger import LOGGER

PLAYER_HOST = "unacadamy-panel-api.vercel.app"
MAX_EVENTS_BYTES = 32 * 1024 * 1024
MAX_SLIDE_BYTES = 16 * 1024 * 1024
MAX_ENCODED_SLIDE_BYTES = 32 * 1024 * 1024
_render_lock = asyncio.Lock()  # One encoder globally on the 1.5-core server.


def extract_password(caption):
    match = re.search(r"\b(?:PASS|PASSWORD)\s*(?::|=|\s)\s*([A-Za-z0-9_-]+)", caption or "", re.I)
    return match.group(1) if match else None


def uses_board_player(url):
    return urlsplit(url).hostname == PLAYER_HOST


class _TokenParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "x-tok":
            self.token = attrs.get("content")


async def _bounded_body(response, limit):
    response.raise_for_status()
    result = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        if len(result) + len(chunk) > limit:
            raise ValueError("Watch Board response exceeds the safe memory limit")
        result.extend(chunk)
    return bytes(result)


def decode_events(payload):
    """Validate the observed API format. Never execute remote code to decode it."""
    events = payload.get("_d", payload.get("events"))
    if not isinstance(events, list) or not events:
        raise ValueError("The player returned no board events; camera-only fallback is disabled")
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("p_time"), (float, int)):
            raise ValueError("Malformed Watch Board timeline")
        if not math.isfinite(event["p_time"]) or event["p_time"] < 0:
            raise ValueError("Invalid Watch Board timestamp")
    return sorted(events, key=lambda event: event["p_time"])


async def load_board_events(player_url, camera_url, password):
    if not password:
        raise ValueError("No [PASS ...] / password found in this post's title or caption")
    if not uses_board_player(player_url):
        raise ValueError("This Watch Board player is not supported")
    camera = urlsplit(camera_url)
    if camera.scheme != "https" or camera.hostname != "uamedia.uacdn.net":
        raise ValueError("Player camera URL is not the supported public media CDN")
    timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Keep the page's session cookie and request token together. Password
        # cookies are only sent to this exact player, never to CDN redirects.
        async with session.get(player_url, allow_redirects=False) as response:
            parser = _TokenParser()
            parser.feed((await _bounded_body(response, 2 * 1024 * 1024)).decode("utf-8"))
        if not parser.token:
            raise ValueError("Player request token is missing; website format may have changed")
        session.cookie_jar.update_cookies({"vid_pass": password})
        async with session.post(
            urljoin(player_url, "/api/load-video"), json={"url": camera_url},
            headers={"X-Req-Token": parser.token, "Referer": player_url},
            allow_redirects=False,
        ) as response:
            if response.status in (401, 403):
                raise ValueError("Player rejected the password/session; no camera-only file will be sent")
            payload = json.loads(await _bounded_body(response, MAX_EVENTS_BYTES))
        if payload.get("error"):
            raise ValueError("Player could not unlock this lesson")
        return decode_events(payload)


@dataclass
class Slide:
    url: str = ""
    background: str = "#0D0D16"
    strokes: list = field(default_factory=list)
    rotation: int = 0


class BoardTimeline:
    """Replay timestamped slide edits and normalized vector ink, not screenshots.

    Keep vector history per slide (including revisits). Only a few decoded slide
    images are cached; importing a thousand-page deck does not fetch all pages.
    """

    def __init__(self, events, size=(1440, 1080)):
        self.events = events
        self.size = size
        self.index = 0
        self.deck = ["initial"]
        self.slides = {"initial": Slide()}
        self.pointer = 0
        self.color, self.mode, self.pen, self.eraser = "#EAEAEA", "marker", 1, 5
        self.active = {}
        self.dirty = True
        self.frame = None

    @property
    def current(self):
        return self.deck[self.pointer]

    def _register(self, data, timestamp):
        sid = str(data.get("uid") or f"{data.get('u', data.get('url', ''))}_{timestamp}")
        self.slides.setdefault(sid, Slide(data.get("u", data.get("url", "")),
                                         data.get("bc", data.get("backgroundColor", "#0D0D16"))))
        return sid

    def advance(self, seconds):
        target = round(seconds * 1_000_000)
        while self.index < len(self.events) and self.events[self.index]["p_time"] <= target:
            event = self.events[self.index]
            self.index += 1
            data = event.get("data") or {}
            kind = data.get("e")
            if event.get("plugin") == "dcn":
                if kind in ("as", "add-slide"):
                    # Multiple additions at one timestamp form one ordered
                    # insertion, rather than reversing the pages in that batch.
                    additions = [self._register(data, event["p_time"])]
                    while self.index < len(self.events):
                        next_event = self.events[self.index]
                        nd = next_event.get("data") or {}
                        if (next_event["p_time"] != event["p_time"] or next_event.get("plugin") != "dcn"
                                or nd.get("e") != kind or nd.get("i") != data.get("i")):
                            break
                        additions.append(self._register(nd, next_event["p_time"]))
                        self.index += 1
                    pos = int(data.get("i", len(self.deck)))
                    pos = len(self.deck) if pos < 0 else pos
                    while len(self.deck) < pos:
                        sid = f"gap-{len(self.deck)}"
                        self.deck.append(sid)
                        self.slides[sid] = Slide()
                    self.deck[pos:pos] = additions
                    if pos <= self.pointer:
                        self.pointer += len(additions)
                elif kind == "sc":
                    value = data.get("s", data.get("slide"))
                    if isinstance(value, dict):
                        sid = self._register(value, event["p_time"])
                        if sid not in self.deck:
                            self.deck.append(sid)
                        self.pointer = self.deck.index(sid)
                    elif (isinstance(value, (int, float)) and 0 <= value <= 10000
                          and int(value) == value):
                        # The player allows a blank board slot to be selected
                        # before its slide is added (observed in real lessons).
                        # Keep that slot's ink history instead of rejecting it.
                        while len(self.deck) <= value:
                            sid = f"jump-{len(self.deck)}"
                            self.deck.append(sid)
                            self.slides[sid] = Slide()
                        self.pointer = int(value)
                    else:
                        raise ValueError(f"Invalid Watch Board slide selection {value!r} at {event['p_time']}, deck size {len(self.deck)}")
                    self.dirty = True
                elif kind == "cc":
                    self.color = data.get("c") or self.color
                elif kind == "mc":
                    self.mode = str(data.get("m") or "marker").lower().replace("-", "").replace("_", "")
                elif kind == "pstc":
                    self.pen = float(data.get("s", self.pen))
                elif kind == "estc":
                    self.eraser = float(data.get("s", self.eraser))
                elif kind == "sbc":
                    self.slides[self.current].background = data.get("bc", data.get("c", "#0D0D16"))
                    self.dirty = True
                elif kind == "rs":
                    self.slides[self.current].rotation = int(data.get("v", 0))
                    self.dirty = True
                elif kind == "ea":
                    self._clear()
            elif event.get("plugin") == "cw":
                self._ink(data)

    def _clear(self):
        self.slides[self.current].strokes.clear()
        self.active = {key: value for key, value in self.active.items() if key[0] != self.current}
        self.dirty = True

    def _ink(self, data):
        inner = data.get("data", data)
        kind = inner.get("e")
        strokes = self.slides[self.current].strokes
        key = (self.current, data.get("id", data.get("canvasId", 0)))
        pt = inner.get("p") or {}
        valid = all(isinstance(pt.get(k), (int, float)) and math.isfinite(pt[k]) for k in ("x", "y"))
        if kind == "cl":
            self._clear()
        elif kind == "un":
            if strokes:
                strokes.pop()
                self.active.pop(key, None)
                self.dirty = True
        elif kind == "dlos":
            # Delete selected ink objects. Do not flatten history into pixels.
            objects = inner.get("oids", inner.get("o", inner.get("ids", [])))
            if isinstance(objects, list):
                strokes[:] = [stroke for stroke in strokes if stroke["id"] not in objects]
                self.dirty = True
        elif kind == "d" and valid and pt.get("shouldDraw") is not False:
            if self.mode == "selection":
                return
            supported = {"marker", "markerh", "highlighter", "eraser", "line", "rectangle", "circle", "ellipse", "arrow"}
            if self.mode not in supported:
                raise ValueError(f"Unsupported Watch Board drawing mode: {self.mode}")
            stroke = {"points": [(pt["x"], pt["y"])], "mode": self.mode, "color": self.color,
                      "size": self.eraser if self.mode == "eraser" else self.pen, "id": pt.get("oid")}
            strokes.append(stroke)
            self.active[key] = stroke
            self.dirty = True
        elif kind in ("m", "u") and key in self.active:
            if valid and pt.get("shouldDraw") is not False:
                self.active[key]["points"].append((pt["x"], pt["y"]))
                self.dirty = True
            if kind == "u":
                self.active.pop(key, None)

    def draw(self, source_image=None):
        slide = self.slides[self.current]
        frame = Image.new("RGB", self.size, slide.background)
        # Point coordinates belong to the fitted slide, not letterbox margins.
        box_size = source_image.size if source_image else (16, 9)
        scale = min(self.size[0] / box_size[0], self.size[1] / box_size[1])
        fitted = (max(1, round(box_size[0] * scale)), max(1, round(box_size[1] * scale)))
        origin = ((self.size[0] - fitted[0]) // 2, (self.size[1] - fitted[1]) // 2)
        if source_image:
            frame.paste(source_image.resize(fitted, Image.Resampling.LANCZOS), origin)
        ink = Image.new("RGBA", self.size)
        for stroke in slide.strokes:
            points = [(round(origin[0] + x * fitted[0]), round(origin[1] + y * fitted[1]))
                      for x, y in stroke["points"]]
            width = max(1, round(stroke["size"] * fitted[0] / 750))
            mode = stroke["mode"]
            rgba = (*ImageColor.getrgb(stroke["color"]), 90 if mode in ("highlighter", "markerh") else 255)
            draw = ImageDraw.Draw(ink)
            if mode == "eraser":
                draw.line(points, fill=(0, 0, 0, 0), width=width, joint="curve")
            elif mode in ("rectangle", "ellipse", "circle"):
                first, last = points[0], points[-1]
                box = (min(first[0], last[0]), min(first[1], last[1]),
                       max(first[0], last[0]), max(first[1], last[1]))
                getattr(draw, "rectangle" if mode == "rectangle" else "ellipse")(box, outline=rgba, width=width)
            else:
                path = [points[0], points[-1]] if mode in ("line", "arrow") else points
                draw.line(path, fill=rgba, width=width, joint="curve")
                radius = width / 2
                for x, y in (path[0], path[-1]):
                    draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=rgba)
                if mode == "arrow" and len(points) > 1:
                    angle = math.atan2(points[-1][1]-points[0][1], points[-1][0]-points[0][0])
                    x, y = points[-1]
                    head = [(x-12*width*math.cos(angle+a), y-12*width*math.sin(angle+a)) for a in (-0.4, 0.4)]
                    draw.line([head[0], (x, y), head[1]], fill=rgba, width=width)
        frame.paste(ink, (0, 0), ink)
        if slide.rotation:
            frame = frame.rotate(-slide.rotation, expand=False, fillcolor=slide.background)
        self.frame = frame.tobytes()
        self.dirty = False
        return self.frame


class PlayerBoardTimeline(BoardTimeline):
    """Match the supported web player's visible ink, only for the new layout.

    Its published UAObEngine.compileTimeline does not compile `dlos` (selected
    object deletion) events. Consequently those annotations remain visible in
    the browser. Keep that behavior here instead of silently dropping notes
    present in the requested player view. Legacy /batch_watch is unaffected.
    """

    def _ink(self, data):
        if (data.get("data", data) or {}).get("e") != "dlos":
            super()._ink(data)


class SlideCache:
    def __init__(self, session, encoded_cache=None):
        self.session, self.cache = session, OrderedDict()
        self.owns_encoded_cache = encoded_cache is None
        self.encoded_cache = OrderedDict() if encoded_cache is None else encoded_cache
        self.encoded_bytes = sum(len(data) for data in self.encoded_cache.values())

    async def get(self, url):
        if not url:
            return None
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "uadoc.uacdn.net":
            raise ValueError("Unsupported slide host; no private/internal URL requests are allowed")
        if url in self.cache:
            self.cache.move_to_end(url)
            return self.cache[url]
        if url in self.encoded_cache:
            self.encoded_cache.move_to_end(url)
            data = self.encoded_cache[url]
        else:
            async with self.session.get(url, allow_redirects=False) as response:
                data = await _bounded_body(response, MAX_SLIDE_BYTES)
            self.encoded_cache[url] = data
            self.encoded_bytes += len(data)
            while self.encoded_bytes > MAX_ENCODED_SLIDE_BYTES:
                self.encoded_bytes -= len(self.encoded_cache.popitem(last=False)[1])
        # uadoc's PDF?page=N URL serves a JPEG, not a complete PDF document.
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("Slide resolution exceeds safe memory limits")
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
        self.cache[url] = image
        while len(self.cache) > 4:
            self.cache.popitem(last=False)[1].close()
        return image

    def close(self):
        for image in self.cache.values():
            image.close()
        self.cache.clear()
        if self.owns_encoded_cache:
            self.encoded_cache.clear()
            self.encoded_bytes = 0


def _media_metadata(path):
    # imageio-ffmpeg supplies a binary on hosts lacking system FFprobe.
    import imageio_ffmpeg
    reader = imageio_ffmpeg.read_frames(path)
    try:
        metadata = next(reader)
    finally:
        reader.close()
    duration = float(metadata.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Cannot determine complete video duration")
    return metadata


async def get_board_video_info(path):
    """Usable duration/dimensions even when the host has no system FFprobe."""
    meta = await asyncio.to_thread(_media_metadata, path)
    width, height = meta["size"]
    return round(meta["duration"]), None, None, width, height


async def native_board_size(events, end_time, cache, maximum, abort_event=None):
    """Preserve the largest source slide, without inflating 760px images to 1920px."""
    timeline = BoardTimeline(events)
    seen = set()
    urls = []
    for event in events:
        if event["p_time"] > end_time * 1_000_000:
            break
        if event.get("plugin") != "dcn" or (event.get("data") or {}).get("e") != "sc":
            continue
        if abort_event and abort_event.is_set():
            raise asyncio.CancelledError
        timeline.advance(event["p_time"] / 1_000_000)
        url = timeline.slides[timeline.current].url
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    width = height = 0

    async def dimensions(url):
        image = await cache.get(url)
        return image.size

    # Four requests maximum; do not serially wait for dozens of slide fetches.
    for offset in range(0, len(urls), 4):
        if abort_event and abort_event.is_set():
            raise asyncio.CancelledError
        tasks = [asyncio.create_task(dimensions(url)) for url in urls[offset:offset+4]]
        try:
            for w, h in await asyncio.gather(*tasks):
                width, height = max(width, w), max(height, h)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    if not width:
        return maximum
    width, height = min(width, maximum[0]), min(height, maximum[1])
    return width + width % 2, height + height % 2


def format_render_progress(current, total, elapsed):
    """Actual render throughput, not a timeline counter mistaken for wall time."""
    from helpers.files import get_readable_time
    elapsed = max(0, elapsed)
    percent = min(100, current / total * 100) if total > 0 else 0
    text = ("🎨 **Exporting Board + Face**\n"
            f"Timeline: `{get_readable_time(int(current))}` / "
            f"`{get_readable_time(int(total))}` ({percent:.1f}%)\n"
            f"⏱ Elapsed: `{get_readable_time(int(elapsed))}`")
    if current > 0 and elapsed > 0:
        rate = current / elapsed
        eta = max(0, (total - current) / rate)
        text += f" • `{rate:.1f}×` realtime\n⌛ Estimated remaining: `{get_readable_time(math.ceil(eta))}`"
    else:
        text += "\nPreparing slides; estimating export time…"
    return text + "\nOriginal slide detail + timed handwriting + teacher."


def board_yuv420(rgb, size):
    """Convert a changed board once, instead of converting RGB every output frame.

    Pillow produces full-range JPEG YCbCr. Map it explicitly to video range so
    FFmpeg's yuv420p input preserves white backgrounds and black text correctly.
    Chroma is subsampled as it is in the final Telegram-compatible H.264 output.
    """
    with Image.frombytes("RGB", size, rgb) as image:
        with image.convert("YCbCr") as ycbcr:
            channels = ycbcr.split()
            try:
                with channels[0].point([round(16 + value*219/255) for value in range(256)]) as y:
                    result = y.tobytes()
                for channel in channels[1:]:
                    with channel.resize((size[0]//2, size[1]//2), Image.Resampling.BOX) as half:
                        with half.point([round(16 + value*224/255) for value in range(256)]) as limited:
                            result += limited.tobytes()
                return result
            finally:
                for channel in channels:
                    channel.close()


async def render_board_video(camera_path, events, dest_path, abort_event=None, progress=None,
                             max_size=2000 * 1048576, *, clip_start=0, clip_duration=None,
                             layout="side_by_side"):
    """Stream a bounded number of raw board frames to FFmpeg; retain no frame files.

    clip_* are for opt-in diagnostics only. Batch callers always render full length.
    Both layouts keep the teacher outside the board so it never covers text.
    The new command uses fast_side_by_side with separate speed settings.
    """
    if layout not in ("side_by_side", "pip", "fast_side_by_side"):
        raise ValueError("Unsupported Watch Board layout")
    async with _render_lock:
        started = monotonic()
        if abort_event and abort_event.is_set():
            raise asyncio.CancelledError
        import imageio_ffmpeg
        meta = await asyncio.to_thread(_media_metadata, camera_path)
        duration = float(meta["duration"])
        if clip_duration is not None:
            duration = min(float(clip_duration), duration - clip_start)
        if duration <= 0 or clip_start < 0:
            raise ValueError("Invalid render duration")
        # Check the whole requested timeline before starting an expensive encode.
        fast = layout == "fast_side_by_side"
        timeline_class = PlayerBoardTimeline if layout != "side_by_side" else BoardTimeline
        await asyncio.to_thread(timeline_class(events).advance, clip_start + duration)
        board_w, height = PyroConf.WATCH_BOARD_WIDTH, PyroConf.WATCH_BOARD_HEIGHT
        encoded_slides = OrderedDict()
        if PyroConf.WATCH_BOARD_NATIVE_SIZE or fast:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                cache = SlideCache(session, encoded_slides)
                try:
                    board_w, height = await native_board_size(events, clip_start + duration, cache,
                                                              (board_w, height), abort_event)
                finally:
                    cache.close()
            # Do not shrink the original teacher track if slides are unusually
            # small. The configured height remains the explicit ceiling.
            height = max(height, min(int(meta["size"][1]), PyroConf.WATCH_BOARD_HEIGHT))
            height += height % 2
        fps = PyroConf.WATCH_BOARD_FPS
        preset, crf = PyroConf.WATCH_BOARD_PRESET, PyroConf.WATCH_BOARD_CRF
        teacher_w = min(int(meta["size"][0]), 640)
        teacher_w += teacher_w % 2
        # Preserve the camera track's frame rate. Board edits update at the
        # configured cadence, independently from the camera's original fps.
        camera_fps = float(meta.get("fps") or 25)
        graph = (f"[0:v]fps={camera_fps},pad={board_w+teacher_w}:{height}:0:0:black[b];"
                 f"[1:v]scale={teacher_w}:{height}:force_original_aspect_ratio=decrease[c];"
                 f"[b][c]overlay=x={board_w}:y=(H-h)/2:shortest=1[v]")
        output_w, output_h = board_w + teacher_w, height
        if fast:
            # Preserve camera resolution and frame rate. Save encoder analysis
            # work, not pixels or frames; CRF stays at the existing quality setting.
            teacher_w = int(meta["size"][0])
            teacher_w += teacher_w % 2
            height = max(height, int(meta["size"][1]))
            height += height % 2
            output_w, output_h = board_w + teacher_w, height
            preset = PyroConf.WATCH_BOARD_VIDEO_PRESET
            graph = (f"[0:v]fps={camera_fps},pad={output_w}:{height}:0:0:black[b];"
                     f"[1:v]scale={teacher_w}:{height}:force_original_aspect_ratio=decrease[c];"
                     f"[b][c]overlay=x={board_w}:y=(H-h)/2:shortest=1[v]")
        if layout == "pip":
            # Main board remains pixel-for-pixel at the chosen resolution.
            # A reserved footer avoids obscuring notes with the small camera.
            teacher_w = min(int(meta["size"][0]), max(2, board_w // 4))
            teacher_w -= teacher_w % 2
            teacher_h = max(2, round(teacher_w * meta["size"][1] / meta["size"][0]))
            teacher_h += teacher_h % 2
            camera_fps = min(camera_fps, PyroConf.WATCH_BOARD_PIP_FPS)
            output_w, output_h = board_w, height + teacher_h + 16
            graph = (f"[0:v]fps={camera_fps},pad={output_w}:{output_h}:0:0:black[b];"
                     f"[1:v]fps={camera_fps},scale={teacher_w}:{teacher_h}[c];"
                     f"[b][c]overlay=x=8:y={height+8}:shortest=1[v]")
        temp_path = dest_path + ".rendering.mp4"
        args = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                "-filter_complex_threads", "1", "-threads", "1", "-thread_queue_size", "2",
                "-probesize", "32", "-analyzeduration", "0", "-fpsprobesize", "0",
                "-f", "rawvideo", "-pixel_format", "yuv420p" if fast else "rgb24",
                "-video_size", f"{board_w}x{height}", "-framerate", str(fps), "-i", "pipe:0",
                "-ss", str(clip_start), "-threads", "2", "-i", camera_path,
                "-filter_complex", graph, "-map", "[v]", "-map", "1:a:0?", "-t", str(duration),
                "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-fs", str(max_size), "-movflags", "+faststart", temp_path]
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.PIPE)
        errors = bytearray()

        async def read_errors():
            while True:
                chunk = await process.stderr.read(4096)
                if not chunk:
                    return
                errors.extend(chunk)
                del errors[:-8192]

        error_task = asyncio.create_task(read_errors())
        reserve = PyroConf.DISK_RESERVE_MIB * 1048576
        timeline = timeline_class(events, (board_w, height))
        frame_payload = None
        last_progress = -math.inf
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                cache = SlideCache(session, encoded_slides)
                try:
                    for number in range(math.ceil(duration * fps)):
                        if abort_event and abort_event.is_set():
                            raise asyncio.CancelledError
                        if number % max(1, math.ceil(fps)) == 0:
                            if shutil.disk_usage(os.path.dirname(os.path.abspath(dest_path))).free < reserve:
                                raise ValueError("Insufficient space for board export (256 MiB reserve)")
                            if os.path.exists(temp_path) and os.path.getsize(temp_path) >= max_size:
                                raise ValueError("Board export exceeds Telegram's destination upload limit")
                        timeline.advance(clip_start + number / fps)
                        if timeline.dirty or timeline.frame is None:
                            source = await cache.get(timeline.slides[timeline.current].url)
                            await asyncio.to_thread(timeline.draw, source)
                            frame_payload = (await asyncio.to_thread(board_yuv420, timeline.frame, timeline.size)
                                             if fast else timeline.frame)
                        process.stdin.write(frame_payload)
                        await asyncio.wait_for(process.stdin.drain(), timeout=120)
                        if progress and monotonic() - last_progress >= 5:
                            await progress(number / fps, duration)
                            last_progress = monotonic()
                finally:
                    cache.close()
                    encoded_slides.clear()
            process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=300)
            await error_task
            if process.returncode:
                raise ValueError("Board encoder failed: " + errors.decode(errors="replace")[-1000:])
            output = await asyncio.to_thread(_media_metadata, temp_path)
            if abs(float(output["duration"]) - duration) > max(0.5, 2 / camera_fps):
                raise ValueError("Board export is truncated; nothing was sent")
            if os.path.getsize(temp_path) > max_size:
                raise ValueError("Board export exceeds Telegram's destination upload limit")
            os.replace(temp_path, dest_path)
            elapsed = monotonic() - started
            LOGGER(__name__).info("Watch Board export: %sx%s, %.2fs, %s bytes, %s slide selections; "
                                  "%.2fs elapsed, %.2fx realtime, preset=%s",
                                  output_w, output_h, duration, os.path.getsize(dest_path),
                                  sum((e.get("data") or {}).get("e") == "sc" for e in events),
                                  elapsed, duration / max(elapsed, 0.001), preset)
            return dest_path
        except (BrokenPipeError, ConnectionResetError) as exc:
            await asyncio.wait_for(process.wait(), timeout=30)
            await error_task
            raise ValueError("Board encoder stopped: " + errors.decode(errors="replace")[-1500:]) from exc
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            process.stdin.close()
            try:
                await asyncio.wait_for(process.stdin.wait_closed(), timeout=5)
            except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                pass
            await error_task
            if os.path.exists(temp_path):
                os.remove(temp_path)
