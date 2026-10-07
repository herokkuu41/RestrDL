import os
import asyncio
import tempfile
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
    MessageEntity,
)
from pyrogram.enums import MessageEntityType

import main
from helpers.watch_board import (
    extract_watch_board_url,
    extract_title_and_filename,
    resolve_video_cdn_url,
    download_watch_board_video,
)
from config import PyroConf


# ==============================================================================
# 1. Tests for extract_watch_board_url
# ==============================================================================
def test_extract_url_from_button_text():
    button = InlineKeyboardButton(
        text="🎬 Watch Board and Face",
        url="https://unacadamy-panel-api.vercel.app/?url=https://uamedia.uacdn.net/lesson-raw/test/output.webm"
    )
    msg = MagicMock(spec=Message)
    msg.reply_markup = InlineKeyboardMarkup([[button]])
    msg.caption_entities = None
    msg.entities = None
    msg.caption = None
    msg.text = None

    url = extract_watch_board_url(msg)
    assert url == "https://unacadamy-panel-api.vercel.app/?url=https://uamedia.uacdn.net/lesson-raw/test/output.webm"


def test_extract_url_from_button_domain():
    button = InlineKeyboardButton(
        text="Click Here",
        url="https://uamedia.uacdn.net/lesson-raw/abc/output.webm"
    )
    msg = MagicMock(spec=Message)
    msg.reply_markup = InlineKeyboardMarkup([[button]])
    msg.caption_entities = None
    msg.entities = None
    msg.caption = None
    msg.text = None

    url = extract_watch_board_url(msg)
    assert url == "https://uamedia.uacdn.net/lesson-raw/abc/output.webm"


def test_extract_url_from_caption_entities():
    msg = MagicMock(spec=Message)
    msg.reply_markup = None
    entity = MessageEntity(
        type=MessageEntityType.TEXT_LINK,
        offset=0,
        length=11,
        url="https://unacadamy-panel-api.vercel.app/?url=https://uamedia.uacdn.net/lesson/output.webm"
    )
    msg.caption_entities = [entity]
    msg.entities = None
    msg.caption = "Watch Board link"
    msg.text = None

    url = extract_watch_board_url(msg)
    assert url == "https://unacadamy-panel-api.vercel.app/?url=https://uamedia.uacdn.net/lesson/output.webm"


def test_extract_url_from_raw_caption():
    msg = MagicMock(spec=Message)
    msg.reply_markup = None
    msg.caption_entities = None
    msg.entities = None
    msg.caption = "Check out this video: https://uamedia.uacdn.net/lesson-raw/xyz/output.webm for details"
    msg.text = None

    url = extract_watch_board_url(msg)
    assert url == "https://uamedia.uacdn.net/lesson-raw/xyz/output.webm"


def test_extract_url_none():
    assert extract_watch_board_url(None) is None

    msg = MagicMock(spec=Message)
    msg.reply_markup = None
    msg.caption_entities = None
    msg.entities = None
    msg.caption = "Just regular text with no link"
    msg.text = None
    assert extract_watch_board_url(msg) is None


def test_extract_url_telegram_link_ignored():
    button = InlineKeyboardButton(
        text="📢 Join Our Channel",
        url="https://t.me/somechannel"
    )
    msg = MagicMock(spec=Message)
    msg.reply_markup = InlineKeyboardMarkup([[button]])
    msg.caption_entities = None
    msg.entities = None
    msg.caption = None
    msg.text = None

    assert extract_watch_board_url(msg) is None


def test_extract_url_whatsapp_invite_ignored():
    button = InlineKeyboardButton(
        text="💬 Join WhatsApp",
        url="https://chat.whatsapp.com/invite123"
    )
    msg = MagicMock(spec=Message)
    msg.reply_markup = InlineKeyboardMarkup([[button]])
    msg.caption_entities = None
    msg.entities = None
    msg.caption = None
    msg.text = None

    assert extract_watch_board_url(msg) is None


# ==============================================================================
# 2. Tests for extract_title_and_filename
# ==============================================================================
def test_extract_title_and_filename_with_pass():
    text = "Title : Rotational Dynamics\nPass: OTBW02"
    title, filename = extract_title_and_filename(text, 101)
    assert title == "Rotational Dynamics [PASS: OTBW02]"
    assert filename == "Rotational Dynamics [PASS_ OTBW02].webm"
    assert ":" not in filename


def test_extract_title_and_filename_inline_pass():
    text = "Title : Physics Chapter 1 [PASS: OTBW02]"
    title, filename = extract_title_and_filename(text, 102)
    assert title == "Physics Chapter 1 [PASS: OTBW02]"
    assert filename == "Physics Chapter 1 [PASS_ OTBW02].webm"


def test_extract_title_sanitization_illegal_chars():
    text = 'Title : Math: 1/2*3? "Quotes" <Tags> | Pipe'
    title, filename = extract_title_and_filename(text, 103)
    assert title == 'Math: 1/2*3? "Quotes" <Tags> | Pipe'
    for bad in '<>:"/\\|?*':
        assert bad not in filename
    assert filename.endswith(".webm")


def test_extract_title_and_filename_fallback():
    title, filename = extract_title_and_filename(None, 456)
    assert title == "watch_board_456"
    assert filename == "watch_board_456.webm"

    title_empty, filename_empty = extract_title_and_filename("", 789)
    assert title_empty == "watch_board_789"
    assert filename_empty == "watch_board_789.webm"


def test_extract_title_and_filename_empty_after_strip():
    text = "Title : ???"
    title, filename = extract_title_and_filename(text, 555)
    assert title == "???"
    assert filename == "watch_board_555.webm"


def test_extract_title_and_filename_very_long():
    text = "Title : " + "A" * 300
    title, filename = extract_title_and_filename(text, 666)
    assert len(filename) <= 200
    assert filename.endswith(".webm")


# ==============================================================================
# 3. Tests for resolve_video_cdn_url
# ==============================================================================
@pytest.mark.asyncio
async def test_resolve_video_cdn_url_direct_param():
    direct_redirect = "https://unacadamy-panel-api.vercel.app/?url=https%3A%2F%2Fuamedia.uacdn.net%2Flesson%2Foutput.webm"
    resolved = await resolve_video_cdn_url(direct_redirect)
    assert resolved == "https://uamedia.uacdn.net/lesson/output.webm"


@pytest.mark.asyncio
async def test_resolve_video_cdn_url_direct_cdn():
    cdn_url = "https://uamedia.uacdn.net/lesson-raw/123/output.webm"
    resolved = await resolve_video_cdn_url(cdn_url)
    assert resolved == cdn_url


@pytest.mark.asyncio
async def test_resolve_video_cdn_url_via_html_body():
    landing_url = "https://short.link/watch123"
    fake_html = """
    <html>
      <body>
        <video src="https://uamedia.uacdn.net/lesson-raw/secret/output.webm"></video>
      </body>
    </html>
    """

    mock_resp = AsyncMock()
    mock_resp.url = landing_url
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = AsyncMock(return_value=fake_html)
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    with patch("aiohttp.ClientSession.get", return_value=mock_resp):
        resolved = await resolve_video_cdn_url(landing_url)
        assert resolved == "https://uamedia.uacdn.net/lesson-raw/secret/output.webm"


@pytest.mark.asyncio
async def test_resolve_video_cdn_url_failure():
    assert await resolve_video_cdn_url("") is None
    assert await resolve_video_cdn_url(None) is None

    mock_resp = AsyncMock()
    mock_resp.url = "https://some-empty-page.com"
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = AsyncMock(return_value="<html>No videos here</html>")
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    with patch("aiohttp.ClientSession.get", return_value=mock_resp):
        assert await resolve_video_cdn_url("https://some-empty-page.com") is None


@pytest.mark.asyncio
async def test_resolve_video_cdn_url_json_escaped():
    landing_url = "https://short.link/json123"
    fake_html = '<script>var video = "https:\\/\\/uamedia.uacdn.net\\/lesson-raw\\/secret\\/output.webm";</script>'

    mock_resp = AsyncMock()
    mock_resp.url = landing_url
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = AsyncMock(return_value=fake_html)
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    with patch("aiohttp.ClientSession.get", return_value=mock_resp):
        resolved = await resolve_video_cdn_url(landing_url)
        assert resolved == "https://uamedia.uacdn.net/lesson-raw/secret/output.webm"


@pytest.mark.asyncio
async def test_resolve_video_cdn_url_direct_video_content_type():
    landing_url = "https://cdn.example.com/stream/output.webm"
    mock_resp = AsyncMock()
    mock_resp.url = landing_url
    mock_resp.headers = {"Content-Type": "video/webm"}
    mock_resp.text = AsyncMock(side_effect=AssertionError("Should not read text of binary video!"))
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    with patch("aiohttp.ClientSession.get", return_value=mock_resp):
        resolved = await resolve_video_cdn_url(landing_url)
        assert resolved == landing_url


# ==============================================================================
# 4. Tests for download_watch_board_video
# ==============================================================================
@pytest.mark.asyncio
async def test_download_watch_board_video_success():
    temp_dir = tempfile.mkdtemp()
    dest_path = os.path.join(temp_dir, "test_output.webm")

    chunk_data = [b"chunk1_", b"chunk2_", b"chunk3"]
    total_size = sum(len(c) for c in chunk_data)

    class MockContent:
        async def iter_chunked(self, size):
            for chunk in chunk_data:
                yield chunk

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Length": str(total_size)}
    mock_resp.content = MockContent()
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    progress_calls = []

    async def fake_progress(current, total, *args):
        progress_calls.append((current, total))

    try:
        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await download_watch_board_video(
                "https://uamedia.uacdn.net/lesson/output.webm",
                dest_path,
                progress=fake_progress,
                progress_args=("arg1",),
            )

            assert result == dest_path
            assert os.path.exists(dest_path)
            with open(dest_path, "rb") as f:
                assert f.read() == b"chunk1_chunk2_chunk3"
            assert len(progress_calls) == len(chunk_data)
            assert progress_calls[-1] == (total_size, total_size)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_download_watch_board_video_abort_event():
    temp_dir = tempfile.mkdtemp()
    dest_path = os.path.join(temp_dir, "test_aborted.webm")
    abort_event = asyncio.Event()

    class MockContent:
        async def iter_chunked(self, size):
            yield b"first_chunk"
            abort_event.set()
            yield b"second_chunk"

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Length": "100"}
    mock_resp.content = MockContent()
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    try:
        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await download_watch_board_video(
                "https://uamedia.uacdn.net/lesson/output.webm",
                dest_path,
                abort_event=abort_event,
            )

            assert result is None
            assert not os.path.exists(dest_path)
            # Ensure no orphaned temp files
            assert len(os.listdir(temp_dir)) == 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_download_watch_board_video_disk_reserve():
    temp_dir = tempfile.mkdtemp()
    dest_path = os.path.join(temp_dir, "test_disk_full.webm")

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Length": "500000000"}  # 500 MB
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    try:
        # Mock free disk space to be lower than DISK_RESERVE_MIB
        from types import SimpleNamespace
        with patch("aiohttp.ClientSession.get", return_value=mock_resp), \
             patch("shutil.disk_usage", return_value=SimpleNamespace(free=100)):
            result = await download_watch_board_video(
                "https://uamedia.uacdn.net/lesson/output.webm",
                dest_path,
            )
            assert result is None
            assert not os.path.exists(dest_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_download_watch_board_video_zero_bytes():
    temp_dir = tempfile.mkdtemp()
    dest_path = os.path.join(temp_dir, "test_empty.webm")

    class EmptyContent:
        async def iter_chunked(self, size):
            return
            yield b""

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.headers = {"Content-Length": "0"}
    mock_resp.content = EmptyContent()
    mock_resp.__aenter__.return_value = mock_resp
    mock_resp.__aexit__.return_value = None

    try:
        with patch("aiohttp.ClientSession.get", return_value=mock_resp):
            result = await download_watch_board_video(
                "https://uamedia.uacdn.net/lesson/output.webm",
                dest_path,
            )
            assert result is None
            assert not os.path.exists(dest_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==============================================================================
# 5. Tests for main.py command routing exclusion and interactive flow
# ==============================================================================
@pytest.mark.asyncio
async def test_batch_watch_command_routing_exclusion():
    """Verify batch_watch and batch_watch_board are excluded from handle_text_and_states."""
    user_id = 99999
    main.BATCH_STATES[user_id] = {"step": "ask_link", "mode": "watch_board"}

    mock_bot = MagicMock()
    mock_msg = MagicMock()
    mock_msg.from_user.id = user_id
    mock_msg.text = "https://t.me/c/123456/10"
    mock_msg.reply = AsyncMock()

    await main.handle_text_and_states(mock_bot, mock_msg)

    # Step should transition to ask_count with mode retained
    assert main.BATCH_STATES[user_id]["step"] == "ask_count"
    assert main.BATCH_STATES[user_id]["mode"] == "watch_board"
    assert main.BATCH_STATES[user_id]["start_link"] == "https://t.me/c/123456/10"

    # Now simulate user sending count
    mock_msg.text = "5"
    with patch("main.process_watch_board_batch", new_callable=AsyncMock) as mock_process:
        await main.handle_text_and_states(mock_bot, mock_msg)
        assert user_id not in main.BATCH_STATES
        mock_process.assert_called_once_with(mock_bot, mock_msg, "https://t.me/c/123456/10", 5)


@pytest.mark.asyncio
async def test_batch_watch_command_start_interactive():
    """Verify /batch_watch command initializes BATCH_STATES with mode='watch_board'."""
    user_id = 88888
    main.BATCH_STATES.pop(user_id, None)

    mock_bot = MagicMock()
    mock_msg = MagicMock()
    mock_msg.from_user.id = user_id
    mock_msg.command = ["batch_watch"]
    mock_msg.reply = AsyncMock()

    await main.batch_watch_command_start(mock_bot, mock_msg)

    assert user_id in main.BATCH_STATES
    assert main.BATCH_STATES[user_id]["step"] == "ask_link"
    assert main.BATCH_STATES[user_id]["mode"] == "watch_board"
    mock_msg.reply.assert_called_once()
    assert "Watch Board" in mock_msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_batch_watch_command_start_inline_arguments():
    """Verify /batch_watch <start_link> <count> launches process directly."""
    user_id = 77777
    main.BATCH_STATES.pop(user_id, None)

    mock_bot = MagicMock()
    mock_msg = MagicMock()
    mock_msg.from_user.id = user_id
    mock_msg.command = ["batch_watch", "https://t.me/c/123/10", "15"]
    mock_msg.reply = AsyncMock()

    with patch("main.process_watch_board_batch", new_callable=AsyncMock) as mock_process:
        await main.batch_watch_command_start(mock_bot, mock_msg)
        assert user_id not in main.BATCH_STATES
        mock_process.assert_called_once_with(mock_bot, mock_msg, "https://t.me/c/123/10", 15)


# ==============================================================================
# 6. Tests for process_watch_board_batch execution
# ==============================================================================
@pytest.mark.asyncio
async def test_process_watch_board_batch_long_caption_entity_clamping():
    """Verify long caption (>1024 chars) has entities clamped and does not crash."""
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock()
    mock_bot.get_me = AsyncMock()
    mock_bot.me = MagicMock()
    mock_bot.me.id = 1111

    user_msg = MagicMock()
    user_msg.from_user.id = 12345
    user_msg.chat.id = 12345
    user_msg.reply = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit=AsyncMock()))

    # Create message with caption > 1024 chars and an entity near the end
    long_caption = "A" * 1100
    late_entity = MessageEntity(type=MessageEntityType.BOLD, offset=1050, length=20)
    early_entity = MessageEntity(type=MessageEntityType.ITALIC, offset=10, length=5)

    post_msg = MagicMock()
    post_msg.id = 50
    post_msg.caption = long_caption
    post_msg.caption_entities = [early_entity, late_entity]
    post_msg.reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Watch Board", url="https://uamedia.uacdn.net/lesson/output.webm")
    ]])
    post_msg.empty = False

    with patch("main.getChatMsgID", return_value=(-100123, 50, None)), \
         patch("main.user.get_messages", new_callable=AsyncMock, return_value=[post_msg]), \
         patch("main.resolve_video_cdn_url", new_callable=AsyncMock, return_value="https://uamedia.uacdn.net/lesson/output.webm"), \
         patch("main.download_watch_board_video", new_callable=AsyncMock, return_value="downloads/wb/test.webm"), \
         patch("main.get_media_info", new_callable=AsyncMock, return_value=(10, None, None, 1920, 1080)), \
         patch("main.get_video_thumbnail", new_callable=AsyncMock, return_value=None), \
         patch("os.path.exists", return_value=True), \
         patch("main.cleanup_download"):

        await main.process_watch_board_batch(mock_bot, user_msg, "https://t.me/c/123/50", 1)

        mock_bot.send_video.assert_called_once()
        call_kwargs = mock_bot.send_video.call_args[1]
        assert len(call_kwargs["caption"]) <= 1024
        # Late entity at offset 1050 should be discarded because caption was truncated to 1023
        assert len(call_kwargs["caption_entities"]) == 1
        assert call_kwargs["caption_entities"][0].offset == 10
        assert call_kwargs["file_name"].endswith(".webm")
        assert len(call_kwargs["file_name"]) <= 200


@pytest.mark.asyncio
async def test_process_watch_board_batch_get_messages_none():
    """Verify process_watch_board_batch handles user.get_messages returning None."""
    mock_bot = MagicMock()
    user_msg = MagicMock()
    user_msg.from_user.id = 12345
    user_msg.chat.id = 12345
    user_msg.reply = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit=AsyncMock()))

    with patch("main.getChatMsgID", return_value=(-100123, 50, None)), \
         patch("main.user.get_messages", new_callable=AsyncMock, return_value=None):

        # Should finish without raising TypeError: 'NoneType' object is not iterable
        await main.process_watch_board_batch(mock_bot, user_msg, "https://t.me/c/123/50", 1)


@pytest.mark.asyncio
async def test_process_watch_board_batch_invalid_count():
    mock_bot = MagicMock()
    user_msg = MagicMock()
    user_msg.reply = AsyncMock()

    await main.process_watch_board_batch(mock_bot, user_msg, "https://t.me/c/123/50", 0)
    user_msg.reply.assert_called_once()
    assert "Count must be at least 1" in user_msg.reply.call_args[0][0]


@pytest.mark.asyncio
async def test_process_watch_board_batch_floodwait_abort():
    from pyrogram.errors import FloodWait
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock(side_effect=FloodWait(value=100))
    user_msg = MagicMock()
    user_msg.from_user.id = 12345
    user_msg.chat.id = 12345
    user_msg.reply = AsyncMock(return_value=MagicMock(delete=AsyncMock(), edit=AsyncMock()))

    post_msg = MagicMock()
    post_msg.id = 50
    post_msg.caption = "Test"
    post_msg.caption_entities = None
    post_msg.reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Watch Board", url="https://uamedia.uacdn.net/lesson/output.webm")
    ]])
    post_msg.empty = False

    with patch("main.getChatMsgID", return_value=(-100123, 50, None)), \
         patch("main.user.get_messages", new_callable=AsyncMock, return_value=[post_msg]), \
         patch("main.resolve_video_cdn_url", new_callable=AsyncMock, return_value="https://uamedia.uacdn.net/lesson/output.webm"), \
         patch("main.download_watch_board_video", new_callable=AsyncMock, return_value="downloads/wb/test.webm"), \
         patch("main.get_media_info", new_callable=AsyncMock, return_value=(10, None, None, 1920, 1080)), \
         patch("main.get_video_thumbnail", new_callable=AsyncMock, return_value=None), \
         patch("os.path.exists", return_value=True), \
         patch("main.cleanup_download"), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        await main.process_watch_board_batch(mock_bot, user_msg, "https://t.me/c/123/50", 1)
        mock_sleep.assert_not_called()
