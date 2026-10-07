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
