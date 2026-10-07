import asyncio
import io
import json
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import imageio_ffmpeg
import pytest
from PIL import Image

import main
from helpers import watch_board_render as renderer
from helpers.watch_board import download_watch_board_video


def event(time, plugin="dcn", **data):
    return {"p_time": time * 1_000_000, "plugin": plugin, "data": data}


def ink(time, kind, **point):
    return event(time, "cw", id=1, data={"e": kind, "p": point})


@pytest.mark.parametrize("caption,password", [
    ("Title [PASS ABC123]", "ABC123"), ("Title [PASS: ABC123]", "ABC123"),
    ("Password : xyz_12", "xyz_12"), ("pass=abc-1", "abc-1"), ("No password", None),
])
def test_extract_password(caption, password):
    assert renderer.extract_password(caption) == password


@pytest.mark.parametrize("payload", [{}, {"_d": []}, {"_d": "unsupported"},
                                    {"_d": [{"p_time": float("nan")}]},
                                    {"_d": [{"p_time": -1}]}, {"_d": ["bad"]}])
def test_invalid_timeline_is_not_camera_fallback(payload):
    with pytest.raises(ValueError):
        renderer.decode_events(payload)


def test_event_sort():
    events = [event(2, e="sc", s=0), event(1, e="cc", c="white")]
    assert renderer.decode_events({"_d": events}) == list(reversed(events))


class Body:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        yield self.data


def response(data, status=200, headers=None):
    result = MagicMock()
    result.status = status
    result.headers = headers or {}
    result.content = Body(data)
    result.__aenter__ = AsyncMock(return_value=result)
    result.__aexit__ = AsyncMock(return_value=False)
    return result


@pytest.mark.asyncio
async def test_password_token_api_flow():
    page = response(b'<meta content="req-test" name="x-tok">')
    api = response(json.dumps({"_d": [event(1, e="sc", s=0)]}).encode())
    session = MagicMock()
    session.get.return_value = page
    session.post.return_value = api
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    url = f"https://{renderer.PLAYER_HOST}/?url=video"
    cdn = "https://uamedia.uacdn.net/test/output.webm"
    with patch.object(renderer.aiohttp, "ClientSession", return_value=session):
        result = await renderer.load_board_events(url, cdn, "TESTPASS")
    assert len(result) == 1
    session.cookie_jar.update_cookies.assert_called_once_with({"vid_pass": "TESTPASS"})
    session.post.assert_called_once_with(f"https://{renderer.PLAYER_HOST}/api/load-video",
                                        json={"url": cdn},
                                        headers={"X-Req-Token": "req-test", "Referer": url},
                                        allow_redirects=False)


@pytest.mark.asyncio
async def test_auth_failure_never_returns_raw_video():
    with patch.object(renderer.aiohttp.ClientSession, "get", return_value=response(b'<meta name="x-tok" content="x">')), \
         patch.object(renderer.aiohttp.ClientSession, "post", return_value=response(b"Forbidden", 403)):
        with pytest.raises(ValueError, match="password/session"):
            await renderer.load_board_events(f"https://{renderer.PLAYER_HOST}/", "https://uamedia.uacdn.net/x", "wrong")


@pytest.mark.asyncio
async def test_missing_password_and_token():
    with pytest.raises(ValueError, match="No .*password"):
        await renderer.load_board_events(f"https://{renderer.PLAYER_HOST}/", "https://uamedia.uacdn.net/x", None)
    with patch.object(renderer.aiohttp.ClientSession, "get", return_value=response(b"<html></html>")):
        with pytest.raises(ValueError, match="token is missing"):
            await renderer.load_board_events(f"https://{renderer.PLAYER_HOST}/", "https://uamedia.uacdn.net/x", "test")


@pytest.mark.asyncio
async def test_private_slide_url_is_rejected():
    cache = renderer.SlideCache(MagicMock())
    for url in ("http://localhost/private", "https://uadoc.uacdn.net.evil.test/a", "file:///etc/passwd"):
        with pytest.raises(ValueError, match="slide host"):
            await cache.get(url)


@pytest.mark.asyncio
async def test_slide_cache_retains_at_most_four_decoded_images():
    data = io.BytesIO()
    Image.new("RGB", (32, 18), "white").save(data, format="JPEG")
    session = MagicMock()
    session.get.side_effect = lambda *_, **__: response(data.getvalue())
    cache = renderer.SlideCache(session)
    for number in range(10):
        await cache.get(f"https://uadoc.uacdn.net/slide-{number}.jpg")
    assert len(cache.cache) == 4
    await cache.get("https://uadoc.uacdn.net/slide-9.jpg")
    assert session.get.call_count == 10
    cache.close()
    assert not cache.cache


@pytest.mark.asyncio
async def test_compressed_slide_cache_reuses_preflight_bytes_for_export():
    from collections import OrderedDict
    data = io.BytesIO()
    Image.new("RGB", (760, 427), "white").save(data, format="JPEG")
    payloads = OrderedDict()
    session = MagicMock()
    session.get.side_effect = lambda *_, **__: response(data.getvalue())
    urls = [f"https://uadoc.uacdn.net/slide-{number}.jpg" for number in range(8)]
    first = renderer.SlideCache(session, payloads)
    for url in urls:
        await first.get(url)
    first.close()
    assert len(payloads) == 8  # Compressed bytes, not decoded frames.
    second_session = MagicMock()
    second = renderer.SlideCache(second_session, payloads)
    for url in reversed(urls):
        assert (await second.get(url)).size == (760, 427)
    assert len(second.cache) == 4
    second_session.get.assert_not_called()
    second.close()


@pytest.mark.asyncio
async def test_compressed_slide_cache_is_bounded_and_released(monkeypatch):
    data = io.BytesIO()
    Image.new("RGB", (32, 18), "white").save(data, format="JPEG")
    limit = 3 * len(data.getvalue())
    monkeypatch.setattr(renderer, "MAX_ENCODED_SLIDE_BYTES", limit)
    session = MagicMock()
    session.get.side_effect = lambda *_, **__: response(data.getvalue())
    cache = renderer.SlideCache(session)
    for number in range(10):
        await cache.get(f"https://uadoc.uacdn.net/slide-{number}.jpg")
        assert cache.encoded_bytes <= limit
        assert sum(map(len, cache.encoded_cache.values())) <= limit
    assert len(cache.encoded_cache) == 3
    cache.close()
    assert cache.encoded_bytes == 0 and not cache.encoded_cache and not cache.cache


@pytest.mark.asyncio
async def test_response_size_limit():
    with pytest.raises(ValueError, match="memory limit"):
        await renderer._bounded_body(response(b"abcd"), 3)


def test_slide_insertion_and_revisit_ink():
    events = [event(0, e="as", i=1, uid="first"), event(0, e="as", i=1, uid="second"),
              event(0.1, e="sc", s=1), ink(0.2, "d", x=0.3, y=0.4),
              ink(0.3, "u", x=0.8, y=0.4), event(0.4, e="sc", s=2), event(0.5, e="sc", s=1)]
    timeline = renderer.BoardTimeline(events, (160, 90))
    timeline.advance(0.3)
    assert timeline.deck == ["initial", "first", "second"]
    first = timeline.draw()
    timeline.advance(0.4)
    assert timeline.draw() != first
    timeline.advance(0.5)
    assert timeline.draw() == first


def test_microsecond_insertions_preserve_source_order():
    events = [event(1, e="as", i=1, uid="page2"), event(1.000001, e="as", i=1, uid="page1"),
              event(2, e="sc", s=1)]
    timeline = renderer.BoardTimeline(events)
    timeline.advance(2)
    assert timeline.current == "page1"


def test_exact_microsecond_selection_is_not_delayed_by_float_roundtrip():
    timestamp = 1_000_001
    timeline = renderer.BoardTimeline([{"p_time": timestamp, "plugin": "dcn", "data": {"e": "sc", "s": 1}}])
    timeline.advance(timestamp / 1_000_000)
    assert timeline.current == "jump-1"


@pytest.mark.asyncio
async def test_native_size_uses_largest_visible_slide_not_entire_deck():
    events = [event(0, e="as", i=1, uid="first", u="first"),
              event(0, e="as", i=1, uid="second", u="second"),
              event(0, e="as", i=1, uid="unused", u="unused"),
              event(1.000001, e="sc", s=1), event(2, e="sc", s=2),
              event(3, e="sc", s=1), event(100, e="sc", s=3)]
    cache = MagicMock()
    sizes = {"first": (760, 427), "second": (1000, 600)}
    cache.get = AsyncMock(side_effect=lambda url: MagicMock(size=sizes[url]))
    assert await renderer.native_board_size(events, 10, cache, (1920, 1080)) == (1000, 600)
    assert [call.args[0] for call in cache.get.await_args_list] == ["first", "second"]


@pytest.mark.parametrize("size,expected", [((760, 427), (760, 428)), ((1920, 1080), (640, 360))])
@pytest.mark.asyncio
async def test_native_size_rounds_even_and_respects_explicit_ceiling(size, expected):
    cache = MagicMock(get=AsyncMock(return_value=MagicMock(size=size)))
    maximum = (1920, 1080) if size == (760, 427) else (640, 360)
    events = [event(0, e="sc", s={"u": "slide"})]
    assert await renderer.native_board_size(events, 1, cache, maximum) == expected


@pytest.mark.asyncio
async def test_blank_board_uses_configured_size_without_image_requests():
    cache = MagicMock(get=AsyncMock())
    assert await renderer.native_board_size([event(0, e="sc", s=0)], 10, cache, (640, 360)) == (640, 360)
    cache.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_slide_scan_has_four_request_limit_and_cancels_siblings():
    events = [event(number, e="sc", s={"uid": str(number), "u": str(number)}) for number in range(9)]
    active = peak = finished = 0
    fail = False

    async def get(url):
        nonlocal active, peak, finished
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            if fail and url == "0":
                raise ValueError("slide unavailable")
            await asyncio.sleep(0.01)
            return MagicMock(size=(760, 427))
        finally:
            active -= 1
            finished += 1

    cache = MagicMock(get=get)
    assert await renderer.native_board_size(events, 10, cache, (1920, 1080)) == (760, 428)
    assert peak == 4 and active == 0 and finished == 9
    fail = True
    finished = 0
    with pytest.raises(ValueError, match="slide unavailable"):
        await renderer.native_board_size(events, 10, cache, (1920, 1080))
    assert active == 0 and finished == 4


@pytest.mark.asyncio
async def test_native_slide_scan_cancellation_awaits_all_downloads():
    entered = asyncio.Event()
    active = 0

    async def get(_):
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    events = [event(number, e="sc", s={"u": str(number)}) for number in range(4)]
    task = asyncio.create_task(renderer.native_board_size(events, 10, MagicMock(get=get), (1920, 1080)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


@pytest.mark.asyncio
async def test_native_slide_scan_honors_batch_abort():
    abort = asyncio.Event()
    abort.set()
    cache = MagicMock(get=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await renderer.native_board_size([event(0, e="sc", s={"u": "slide"})], 1, cache, (1920, 1080), abort)
    cache.get.assert_not_awaited()


def test_render_progress_reports_elapsed_rate_and_estimate():
    text = renderer.format_render_progress(120, 600, 20)
    assert "20.0%" in text
    assert "Elapsed: `20s`" in text
    assert "`6.0×` realtime" in text
    assert "Estimated remaining: `1m20s`" in text
    assert "Preparing slides" in renderer.format_render_progress(0, 600, 0)
    assert "Estimated remaining: `0s`" in renderer.format_render_progress(600, 600, 20)


def test_selecting_future_blank_slot_retains_ink():
    events = [event(0, e="sc", s=532), ink(0.1, "d", x=0.5, y=0.5),
              event(0.2, e="sc", s=0), event(0.3, e="sc", s=532)]
    timeline = renderer.BoardTimeline(events)
    timeline.advance(0.3)
    assert timeline.current == "jump-532"
    assert len(timeline.slides[timeline.current].strokes) == 1


def test_eraser_reveals_original_slide_and_undo_restores_ink():
    events = [event(0, e="cc", c="red"), event(0, e="pstc", s=30),
              ink(0.1, "d", x=0.2, y=0.5), ink(0.2, "u", x=0.8, y=0.5),
              event(0.3, e="mc", m="eraser"), event(0.3, e="estc", s=40),
              ink(0.4, "d", x=0.4, y=0.5), ink(0.5, "u", x=0.6, y=0.5),
              event(0.6, "cw", id=1, data={"e": "un"})]
    timeline = renderer.BoardTimeline(events, (150, 90))
    slide = Image.new("RGB", (150, 90), "blue")
    timeline.advance(0.2)
    drawn = timeline.draw(slide)
    timeline.advance(0.5)
    erased = Image.frombytes("RGB", timeline.size, timeline.draw(slide))
    assert erased.getpixel((75, 45)) == (0, 0, 255)
    timeline.advance(0.6)
    assert timeline.draw(slide) == drawn


def test_selection_and_clear():
    timeline = renderer.BoardTimeline([event(0, e="mc", m="selection"), ink(1, "d", x=0.5, y=0.5)])
    timeline.advance(2)
    assert not timeline.slides[timeline.current].strokes


@pytest.mark.parametrize("status,headers", [
    (200, {"Content-Length": "100"}),
    (206, {"Content-Length": "3", "Content-Range": "bytes 0-2/100"}),
    (200, {"Content-Type": "text/html"}),
])
@pytest.mark.asyncio
async def test_partial_or_html_download_is_never_sent(tmp_path, status, headers):
    with patch("aiohttp.ClientSession.get", return_value=response(b"abc", status, headers)):
        assert await download_watch_board_video("https://uamedia.uacdn.net/test", str(tmp_path / "camera.webm")) is None
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def camera(tmp_path):
    path = str(tmp_path / "source.mp4")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=green:size=160x90:rate=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.5",
                    "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", path], check=True)
    return path


@pytest.mark.asyncio
async def test_real_encoder_full_duration_dimensions_audio_and_ink(tmp_path, camera, monkeypatch):
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_WIDTH", 640)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_HEIGHT", 360)
    output = str(tmp_path / "output.mp4")
    events = [event(0, e="cc", c="red"), ink(0, "d", x=0.2, y=0.5), ink(0.2, "u", x=0.8, y=0.5)]
    await renderer.render_board_video(camera, events, output)
    meta = renderer._media_metadata(output)
    assert meta["size"] == (800, 360)
    assert abs(meta["duration"] - 1.5) < 0.2
    assert meta["audio_codec"].startswith("aac")
    assert abs(meta["fps"] - 12) < 0.1
    assert not list(tmp_path.glob("*.rendering.mp4"))


@pytest.mark.parametrize("native,expected", [(True, (320, 90)), (False, (800, 360))])
@pytest.mark.asyncio
async def test_real_encoder_native_resolution_or_explicit_legacy_size(tmp_path, camera, monkeypatch, native, expected):
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_WIDTH", 640)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_HEIGHT", 360)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_NATIVE_SIZE", native)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_PRESET", "veryfast")
    events = [event(0, e="sc", s={"u": "https://uadoc.uacdn.net/slide.jpg"}),
              event(0, e="cc", c="red"), ink(0, "d", x=0.2, y=0.5), ink(0.2, "u", x=0.8, y=0.5)]
    data = io.BytesIO()
    Image.new("RGB", (160, 90), "white").save(data, format="JPEG")
    path = str(tmp_path / "native.mp4")
    with patch.object(renderer.aiohttp.ClientSession, "get", return_value=response(data.getvalue())):
        await renderer.render_board_video(camera, events, path)
    meta = renderer._media_metadata(path)
    assert meta["size"] == expected
    assert abs(meta["duration"] - 1.5) < 0.2
    reader = imageio_ffmpeg.read_frames(path, pix_fmt="rgb24")
    try:
        next(reader)
        # Inspect after the stroke's 0.2s endpoint, not its first blank frame.
        for _ in range(7):
            pixels = next(reader)
        frame = Image.frombytes("RGB", expected, pixels)
    finally:
        reader.close()
    # Visible original slide, red ink, and separate green camera at both sizes.
    board_width = expected[0] - 160
    red, green, blue = frame.getpixel((board_width // 2, expected[1] // 2))
    assert red > 150 and green < 100 and blue < 100
    red, green, blue = frame.getpixel((board_width + 80, expected[1] // 2))
    assert green > red + 40 and green > blue + 40


@pytest.mark.asyncio
async def test_encoders_are_globally_serialized(tmp_path, camera, monkeypatch):
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_WIDTH", 640)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_HEIGHT", 360)
    original_spawn = asyncio.create_subprocess_exec
    active = peak = 0

    async def spawn(*args, **kwargs):
        nonlocal active, peak
        process = await original_spawn(*args, **kwargs)
        active += 1
        peak = max(peak, active)
        original_wait = process.wait
        counted = False
        async def wait():
            nonlocal active, counted
            value = await original_wait()
            if not counted:
                active -= 1
                counted = True
            return value
        process.wait = wait
        return process

    monkeypatch.setattr(renderer.asyncio, "create_subprocess_exec", spawn)
    await asyncio.gather(*(renderer.render_board_video(camera, [], str(tmp_path / f"output-{i}.mp4"))
                           for i in range(2)))
    assert peak == 1
    assert active == 0


@pytest.mark.asyncio
async def test_render_cancellation_reaps_encoder_and_removes_partial(tmp_path, camera, monkeypatch):
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_WIDTH", 640)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_HEIGHT", 360)
    abort = asyncio.Event()
    async def progress(*_):
        abort.set()
    with pytest.raises(asyncio.CancelledError):
        await renderer.render_board_video(camera, [], str(tmp_path / "output.mp4"), abort, progress)
    assert not (tmp_path / "output.mp4").exists()
    assert not list(tmp_path.glob("*.rendering.mp4"))


@pytest.mark.asyncio
async def test_low_disk_aborts_without_camera_fallback(tmp_path, camera):
    with patch.object(renderer.shutil, "disk_usage", return_value=MagicMock(free=1)):
        with pytest.raises(ValueError, match="Insufficient space"):
            await renderer.render_board_video(camera, [], str(tmp_path / "output.mp4"))
    assert not (tmp_path / "output.mp4").exists()
    assert not list(tmp_path.glob("*.rendering.mp4"))


@pytest.mark.asyncio
async def test_batch_sends_rendered_file_not_camera(tmp_path):
    source = tmp_path / "lecture.webm"
    exported = tmp_path / "lecture.mp4"
    source.touch()
    exported.touch()
    post = MagicMock(id=10, caption="Title: Lecture [PASS: TEST]", empty=False, caption_entities=None)
    request = MagicMock()
    request.from_user.id = 76543
    request.chat.id = 76543
    request.reply = AsyncMock(return_value=MagicMock(edit=AsyncMock(), delete=AsyncMock()))
    client = MagicMock(send_video=AsyncMock())
    with patch.object(main, "getChatMsgID", return_value=(-100123, 10, None)), \
         patch.object(main.user, "get_messages", AsyncMock(return_value=[post])), \
         patch.object(main, "extract_watch_board_url", return_value=f"https://{renderer.PLAYER_HOST}/?url=test"), \
         patch.object(main, "resolve_video_cdn_url", AsyncMock(return_value="https://uamedia.uacdn.net/test")), \
         patch.object(main, "load_board_events", AsyncMock(return_value=[event(0, e="sc", s=0)])) as load, \
         patch.object(main, "download_watch_board_video", AsyncMock(return_value=str(source))), \
         patch.object(main, "render_board_video", AsyncMock(return_value=str(exported))) as render, \
         patch.object(main, "get_media_info", AsyncMock(return_value=(10, None, None, 2560, 1080))), \
         patch.object(main, "get_video_thumbnail", AsyncMock(return_value=None)), \
         patch.object(main, "cleanup_download") as cleanup:
        await main.process_watch_board_batch(client, request, "https://t.me/c/123/10", 1)
    assert load.await_args.args[2] == "TEST"
    assert render.await_args.args[0] == str(source)
    assert client.send_video.await_args.kwargs["video"] == str(exported)
    assert client.send_video.await_args.kwargs["file_name"].endswith(".mp4")
    assert cleanup.call_count == 2


@pytest.mark.parametrize("stage", ["password", "render"])
@pytest.mark.asyncio
async def test_batch_export_failure_does_not_send_camera(tmp_path, stage):
    source = tmp_path / "lecture.webm"
    source.touch()
    post = MagicMock(id=10, caption="Title: Lecture [PASS TEST]", empty=False, caption_entities=None)
    request = MagicMock()
    request.from_user.id = 76543
    request.chat.id = 76543
    request.reply = AsyncMock(return_value=MagicMock(edit=AsyncMock(), delete=AsyncMock()))
    client = MagicMock(send_video=AsyncMock())
    load = AsyncMock(side_effect=ValueError("Incorrect password") if stage == "password" else None,
                     return_value=[event(0, e="sc", s=0)])
    render = AsyncMock(side_effect=ValueError("Export failed"))
    with patch.object(main, "getChatMsgID", return_value=(-100123, 10, None)), \
         patch.object(main.user, "get_messages", AsyncMock(return_value=[post])), \
         patch.object(main, "extract_watch_board_url", return_value=f"https://{renderer.PLAYER_HOST}/?url=test"), \
         patch.object(main, "resolve_video_cdn_url", AsyncMock(return_value="https://uamedia.uacdn.net/test")), \
         patch.object(main, "load_board_events", load), \
         patch.object(main, "download_watch_board_video", AsyncMock(return_value=str(source))) as download, \
         patch.object(main, "render_board_video", render), \
         patch.object(main, "cleanup_download"):
        await main.process_watch_board_batch(client, request, "https://t.me/c/123/10", 1)
    client.send_video.assert_not_awaited()
    if stage == "password":
        download.assert_not_awaited()
    else:
        render.assert_awaited_once()


@pytest.mark.asyncio
async def test_bundled_ffmpeg_metadata_without_ffprobe(camera):
    duration, _, _, width, height = await renderer.get_board_video_info(camera)
    assert duration > 0
    assert (width, height) == (160, 90)
