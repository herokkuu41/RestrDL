import asyncio
import io
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import imageio_ffmpeg
import pytest
from PIL import Image
from pyrogram.enums import MessageEntityType
from pyrogram.errors import FloodWait
from pyrogram.types import MessageEntity

import main
from helpers import watch_board_pip as pip, watch_board_render as renderer
from test_watch_board_render import response, event, ink


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    post = SimpleNamespace(id=10, caption="Title: Lecture [PASS TEST]", text=None,
                           caption_entities=[], empty=False, video=None, document=None,
                           message_thread_id=None)
    request = MagicMock()
    request.from_user.id = 654321
    request.reply = AsyncMock(return_value=MagicMock(edit=AsyncMock(), delete=AsyncMock()))
    client = SimpleNamespace(me=SimpleNamespace(is_premium=False), send_video=AsyncMock(return_value=object()))
    source = SimpleNamespace(get_messages=AsyncMock(return_value=[post]))

    async def download(url, path, **kwargs):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as file:
            file.write(b"camera")
        return path

    async def render(camera, events, output, *args, **kwargs):
        with open(output, "wb") as file:
            file.write(b"composed")
        return output

    monkeypatch.setattr(pip, "getChatMsgID", MagicMock(return_value=(-100123, 10, None)))
    monkeypatch.setattr(pip, "extract_watch_board_url", MagicMock(return_value=f"https://{renderer.PLAYER_HOST}/?url=test"))
    monkeypatch.setattr(pip, "resolve_video_cdn_url", AsyncMock(return_value="https://uamedia.uacdn.net/source"))
    monkeypatch.setattr(pip, "load_board_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(pip, "download_watch_board_video", AsyncMock(side_effect=download))
    monkeypatch.setattr(pip, "render_board_video", AsyncMock(side_effect=render))
    monkeypatch.setattr(pip, "get_board_video_info", AsyncMock(return_value=(120, None, None, 760, 552)))
    monkeypatch.setattr(pip, "get_video_thumbnail", AsyncMock(return_value=None))
    monkeypatch.setattr(pip.PyroConf, "FLOOD_WAIT_DELAY", 0)
    return SimpleNamespace(post=post, request=request, client=client, source=source,
                           abort=asyncio.Event(), notify=AsyncMock(), directory=tmp_path)


async def run(batch, count=1):
    return await pip.run_pip_batch(batch.client, batch.source, batch.request,
                                   "https://t.me/c/123/10", count, target_chat_id=-100456,
                                   abort_event=batch.abort, notify=batch.notify)


@pytest.mark.asyncio
async def test_pip_uses_password_url_and_own_layout_not_telegram_media(batch):
    await run(batch)
    assert pip.load_board_events.await_args.args[2] == "TEST"
    assert pip.render_board_video.await_args.kwargs["layout"] == "pip"
    assert "clip_duration" not in pip.render_board_video.await_args.kwargs
    sent = batch.client.send_video.await_args.kwargs
    assert sent["chat_id"] == -100456 and sent["file_name"].endswith(".mp4")
    assert (sent["duration"], sent["width"], sent["height"]) == (120, 760, 552)
    assert sent["caption"] == batch.post.caption
    assert sent["supports_streaming"]
    assert "Sent: `1`" in batch.request.reply.await_args.args[0]
    assert list((batch.directory / "downloads").iterdir()) == []


@pytest.mark.parametrize("stage", ["password", "download", "render", "metadata", "upload", "no_ack"])
@pytest.mark.asyncio
async def test_failures_never_send_camera_and_cleanup_owned_files(batch, stage):
    if stage == "no_ack":
        batch.client.send_video.return_value = None
    elif stage == "upload":
        batch.client.send_video.side_effect = ValueError("Upload failed")
    else:
        target = {"password": "load_board_events", "download": "download_watch_board_video",
                  "render": "render_board_video", "metadata": "get_board_video_info"}[stage]
        getattr(pip, target).side_effect = ValueError("Test failure")
    await run(batch)
    assert "Sent: `0`" in batch.request.reply.await_args.args[0]
    assert "Failed: `1`" in batch.request.reply.await_args.args[0]
    if stage == "password":
        pip.download_watch_board_video.assert_not_awaited()
    if stage not in ("upload", "no_ack"):
        batch.client.send_video.assert_not_awaited()
    if (batch.directory / "downloads").exists():
        assert list((batch.directory / "downloads").iterdir()) == []


@pytest.mark.asyncio
async def test_caption_and_filename_password_fallback(batch):
    batch.post.caption = "Title: Original lecture"
    batch.post.document = SimpleNamespace(file_name="original [PASS FILEPASS].webm")
    await run(batch)
    assert pip.load_board_events.await_args.args[2] == "FILEPASS"
    assert batch.client.send_video.await_args.kwargs["file_name"] == "Original lecture.mp4"


@pytest.mark.parametrize("stage", ["read", "upload"])
@pytest.mark.asyncio
async def test_floodwait_stops_without_request_retries(batch, stage):
    if stage == "read":
        batch.source.get_messages.side_effect = FloodWait(12)
    else:
        batch.client.send_video.side_effect = FloodWait(12)
    await run(batch)
    assert batch.abort.is_set()
    assert batch.source.get_messages.await_count == 1
    assert batch.client.send_video.await_count <= 1
    assert "Stopped (FloodWait)" in batch.request.reply.await_args.args[0]


@pytest.mark.parametrize("stage", ["download", "render", "upload"])
@pytest.mark.asyncio
async def test_cancel_propagates_and_cleans_up(batch, stage):
    target = {"download": pip.download_watch_board_video, "render": pip.render_board_video,
              "upload": batch.client.send_video}[stage]
    target.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run(batch)
    assert "Cancelled" in batch.request.reply.await_args.args[0]
    if (batch.directory / "downloads").exists():
        assert list((batch.directory / "downloads").iterdir()) == []


@pytest.mark.asyncio
async def test_abort_between_render_and_send_prevents_upload(batch):
    async def render(*args, **kwargs):
        batch.abort.set()
        return "unused.mp4"
    pip.render_board_video.side_effect = render
    await run(batch)
    batch.client.send_video.assert_not_awaited()
    assert "Stopped" in batch.request.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_skips_empty_missing_url_wrong_thread_and_sorts_posts(batch):
    batch.source.get_messages.return_value = [SimpleNamespace(**{**vars(batch.post), "id": 13}),
        SimpleNamespace(**{**vars(batch.post), "id": 11, "empty": True}),
        SimpleNamespace(**{**vars(batch.post), "id": 12}), batch.post]
    pip.extract_watch_board_url.side_effect = lambda post: None if post.id == 12 else f"https://{renderer.PLAYER_HOST}/?url=test"
    await run(batch, 4)
    assert "Sent: `2`" in batch.request.reply.await_args.args[0]
    assert "Skipped: `2`" in batch.request.reply.await_args.args[0]
    assert "post `10`" in batch.request.reply.await_args_list[1].args[0]
    pip.getChatMsgID.return_value = (-100123, 10, 99)
    batch.client.send_video.reset_mock()
    await run(batch, 4)
    batch.client.send_video.assert_not_awaited()


def test_caption_preserves_utf16_offsets_and_does_not_mutate_entities():
    entity = MessageEntity(type=MessageEntityType.BOLD, offset=2, length=1100)
    text, entities = pip.source_caption(SimpleNamespace(caption="🎞" + "x"*1100, text=None,
                                                       caption_entities=[entity]), "unused")
    assert len(text.encode("utf-16-le")) <= 2048
    assert entities[0].offset == 2
    assert entities[0].length == len(text.encode("utf-16-le"))//2 - 2
    assert entity.length == 1100


def test_new_layout_matches_browser_ink_without_changing_legacy_deletions():
    events = [ink(0, "d", x=0.2, y=0.5, oid="underline"),
              ink(0.1, "u", x=0.8, y=0.5),
              event(0.2, "cw", id=1, data={"e": "dlos", "ids": ["underline"]})]
    original = renderer.BoardTimeline(events)
    player = renderer.PlayerBoardTimeline(events)
    original.advance(1)
    player.advance(1)
    assert len(original.slides[original.current].strokes) == 0
    assert len(player.slides[player.current].strokes) == 1
    assert player.slides[player.current].strokes[0]["points"] == [(0.2, 0.5), (0.8, 0.5)]


@pytest.mark.asyncio
async def test_new_command_guided_state_and_own_routing():
    request = MagicMock(command=["batch_watch_video"], text="/batch_watch_video")
    request.from_user.id = 654321
    request.reply = AsyncMock()
    main.BATCH_STATES.pop(request.from_user.id, None)
    with patch.object(main, "release_pending_prompt", AsyncMock()):
        await main.batch_watch_video_command_start(MagicMock(), request)
    assert main.BATCH_STATES[request.from_user.id]["mode"] == "watch_pip"
    request.text = "https://t.me/c/123/10"
    await main.handle_text_and_states(MagicMock(), request)
    request.text = "2"
    with patch.object(main, "track_task", side_effect=lambda coro: coro.close()) as track, \
         patch.object(main, "process_watch_board_batch", AsyncMock()) as old:
        await main.handle_text_and_states(MagicMock(), request)
    track.assert_called_once()
    old.assert_not_called()
    assert request.from_user.id not in main.BATCH_STATES


@pytest.mark.asyncio
async def test_new_command_inline_and_active_batch_guard():
    request = MagicMock(command=["batch_watch_pip", "https://t.me/c/123/10", "2"])
    request.from_user.id = 654321
    request.reply = AsyncMock()
    with patch.object(main, "release_pending_prompt", AsyncMock()), \
         patch.object(main, "track_task", side_effect=lambda coro: coro.close()) as track:
        await main.batch_watch_video_command_start(MagicMock(), request)
        track.assert_called_once()
        main.ACTIVE_BATCHES[request.from_user.id] = {}
        try:
            await main.batch_watch_video_command_start(MagicMock(), request)
            assert track.call_count == 1
        finally:
            main.ACTIVE_BATCHES.pop(request.from_user.id, None)


@pytest.mark.asyncio
async def test_wrapper_resolves_destination_and_cleans_active_state():
    request = MagicMock()
    request.from_user.id = 654321
    with patch.object(main, "resolve_target_chat_id", AsyncMock(return_value=-100456)), \
         patch.object(main, "run_pip_batch", AsyncMock()) as process:
        await main.process_watch_video_batch(MagicMock(), request, "link", 2)
        assert process.await_args.kwargs["target_chat_id"] == -100456
        process.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await main.process_watch_video_batch(MagicMock(), request, "link", 2)
    assert request.from_user.id not in main.ACTIVE_BATCHES


@pytest.mark.parametrize("layout,limit,expected,fps", [
    ("pip", 12, (160, 128), 12), ("pip", 30, (160, 128), 24),
    ("side_by_side", 6, (320, 90), 24),
])
@pytest.mark.asyncio
async def test_real_encoder_layout_ink_audio_duration_and_default_unchanged(tmp_path, monkeypatch, layout, limit, expected, fps):
    camera = str(tmp_path / "camera.mp4")
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=green:size=160x90:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1.5",
                    "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", camera], check=True)
    monkeypatch.setattr(renderer.PyroConf, "WATCH_BOARD_PIP_FPS", limit)
    data = io.BytesIO()
    Image.new("RGB", (160, 90), "white").save(data, format="JPEG")
    events = [event(0, e="sc", s={"u": "https://uadoc.uacdn.net/slide.jpg"}),
              event(0, e="cc", c="red"), ink(0, "d", x=0.2, y=0.5), ink(0.2, "u", x=0.8, y=0.5)]
    output = str(tmp_path / "result.mp4")
    with patch.object(renderer.aiohttp.ClientSession, "get", return_value=response(data.getvalue())):
        await renderer.render_board_video(camera, events, output, layout=layout)
    meta = renderer._media_metadata(output)
    assert meta["size"] == expected
    assert abs(meta["fps"] - fps) < 0.1
    assert abs(meta["duration"] - 1.5) < 0.2
    assert meta["audio_codec"].startswith("aac")
    reader = imageio_ffmpeg.read_frames(output, pix_fmt="rgb24")
    try:
        next(reader)
        for _ in range(7):
            pixels = next(reader)
        frame = Image.frombytes("RGB", expected, pixels)
    finally:
        reader.close()
    assert min(frame.getpixel((80, 10))) > 235  # source board still unobscured
    red, green, blue = frame.getpixel((80, 45))
    assert red > 150 and green < 100 and blue < 100  # timed handwriting included
    pixel = frame.getpixel((28, 109) if layout == "pip" else (240, 45))
    assert pixel[1] > pixel[0] + 40 and pixel[1] > pixel[2] + 40
    if layout == "pip":
        assert max(frame.getpixel((100, 110))) < 20  # rest of footer, not stretched camera
    assert not list(tmp_path.glob("*.rendering.mp4"))


@pytest.mark.asyncio
async def test_invalid_layout_rejected_before_encoder():
    with patch.object(renderer.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
        with pytest.raises(ValueError, match="layout"):
            await renderer.render_board_video("unused", [], "unused", layout="unknown")
    spawn.assert_not_awaited()
