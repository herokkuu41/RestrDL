"""Independent board-first batch mode; does not use/change /batch_watch's loop."""
import asyncio
import os
import tempfile
from copy import copy
from time import monotonic, time

from pyrogram.enums import ParseMode
from pyrogram.errors import AuthKeyDuplicated, FloodWait

from config import PyroConf
from helpers.msg import getChatMsgID
from helpers.utils import progressArgs, progress_for_pyrogram, get_video_thumbnail
from helpers.watch_board import (
    extract_watch_board_url, extract_title_and_filename, resolve_video_cdn_url,
    download_watch_board_video,
)
from helpers.watch_board_render import (
    uses_board_player, extract_password, load_board_events, render_board_video,
    get_board_video_info, format_render_progress,
)
from logger import LOGGER


def source_caption(post, title):
    """Telegram entity offsets and caption limits are measured in UTF-16 units."""
    text = post.caption or post.text or title
    entities = (getattr(post, "caption_entities", None) if post.caption
                else getattr(post, "entities", None)) or []
    encoded = text.encode("utf-16-le")
    if len(encoded) > 2048:
        text = encoded[:2042].decode("utf-16-le", errors="ignore") + "..."
    length = len(text.encode("utf-16-le")) // 2
    result = []
    for entity in entities:
        if entity.offset < length:
            entity = copy(entity)
            entity.length = min(entity.length, length - entity.offset)
            if entity.length > 0:
                result.append(entity)
    return text, result


async def run_pip_batch(bot, source_client, message, start_link, count, *,
                        target_chat_id, abort_event, notify):
    """Authenticate, compose original assets, and send acknowledged full videos.

    Serial files + the renderer's shared global encoder lock bound CPU/memory.
    Temporary files live in a unique owned directory and are always removed.
    """
    processed = skipped = failed = 0
    status = "Complete"
    loading = None
    try:
        if count <= 0:
            raise ValueError("Count must be positive")
        start_chat, start_id, thread_id = getChatMsgID(start_link.split("?")[0].split("#")[0].strip())
        loading = await message.reply(
            "🎞 **Board-first Video Batch**\n"
            f"🔗 Posts `{start_id}` → `{start_id+count-1}`\n"
            "📝 Board on the left • teacher on the right\n"
            "⚙ Source teacher resolution and frame rate; encoding required.\n"
            "Existing /batch_watch is unchanged."
        )
        upload_limit = (4000 if getattr(getattr(bot, "me", None), "is_premium", False)
                        else 2000) * 1048576
        for offset in range(0, count, 50):
            if abort_event.is_set():
                break
            ids = list(range(start_id + offset, start_id + min(offset + 50, count)))
            posts = await source_client.get_messages(chat_id=start_chat, message_ids=ids)
            if not posts:
                posts = []
            elif getattr(posts, "id", None) is not None:
                posts = [posts]
            for post in sorted(posts, key=lambda item: item.id):
                if abort_event.is_set():
                    break
                if post.empty or (thread_id and getattr(post, "message_thread_id", None) != thread_id):
                    skipped += 1
                    continue
                url = extract_watch_board_url(post)
                if not url:
                    skipped += 1
                    continue
                progress = None
                try:
                    if not uses_board_player(url):
                        raise ValueError("This board-first command only supports the verified board player")
                    progress = await message.reply(f"🔓 **Unlocking board assets:** post `{post.id}`")
                    camera_url = await resolve_video_cdn_url(url)
                    if not camera_url:
                        raise ValueError("Could not resolve the source video URL")
                    raw_caption = post.caption or post.text or ""
                    media = getattr(post, "video", None) or getattr(post, "document", None)
                    password = extract_password(raw_caption) or extract_password(getattr(media, "file_name", ""))
                    events = await load_board_events(url, camera_url, password)
                    if abort_event.is_set():
                        break
                    title, filename = extract_title_and_filename(raw_caption, post.id)
                    filename = os.path.splitext(filename)[0] + ".mp4"
                    os.makedirs("downloads", exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="pip_", dir="downloads") as directory:
                        download_started = monotonic()
                        camera = await download_watch_board_video(
                            camera_url, os.path.join(directory, "camera.webm"),
                            abort_event=abort_event, max_size=upload_limit,
                            progress=progress_for_pyrogram,
                            progress_args=progressArgs("Downloading source video", progress, time()),
                        )
                        if abort_event.is_set():
                            break
                        if not camera or not os.path.isfile(camera):
                            raise ValueError("Source download failed; no Telegram-camera fallback was sent")
                        download_elapsed = monotonic() - download_started
                        started = monotonic()

                        async def render_progress(current, total):
                            try:
                                await progress.edit(format_render_progress(current, total, monotonic()-started)
                                                    + "\n🖼 Board left • teacher right")
                            except Exception:
                                pass

                        output = await render_board_video(
                            camera, events, os.path.join(directory, filename), abort_event,
                            render_progress, max_size=upload_limit, layout="fast_side_by_side",
                        )
                        render_elapsed = monotonic() - started
                        if abort_event.is_set():
                            break
                        duration, _, _, width, height = await get_board_video_info(output)
                        thumbnail = await get_video_thumbnail(
                            output, duration, output_path=os.path.join(directory, "thumbnail.jpg"),
                        )
                        caption, entities = source_caption(post, title)
                        kwargs = dict(
                            chat_id=target_chat_id, video=output, file_name=filename,
                            caption=caption, caption_entities=entities, parse_mode=ParseMode.DISABLED,
                            duration=duration, width=width, height=height, supports_streaming=True,
                            progress=progress_for_pyrogram,
                            progress_args=progressArgs("Uploading board-first video", progress, time()),
                        )
                        if thumbnail and os.path.isfile(thumbnail):
                            kwargs["thumb"] = thumbnail
                        if abort_event.is_set():
                            break
                        upload_started = monotonic()
                        sent = await bot.send_video(**kwargs)
                        if not sent:
                            raise ValueError("Telegram did not acknowledge the sent video")
                        processed += 1
                        LOGGER(__name__).info(
                            "Watch video post %s: download=%.2fs render=%.2fs upload=%.2fs "
                            "duration=%ss size=%s bytes preset=%s",
                            post.id, download_elapsed, render_elapsed, monotonic()-upload_started,
                            duration, os.path.getsize(output),
                            PyroConf.WATCH_BOARD_VIDEO_PRESET,
                        )
                        LOGGER(__name__).info("Board-first post %s sent from URL assets, not Telegram media", post.id)
                except (FloodWait, AuthKeyDuplicated, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    failed += 1
                    await notify(message, f"❌ **Post `{post.id}` failed:** `{exc}`\nNo camera-only fallback sent.")
                finally:
                    if progress:
                        try:
                            await progress.delete()
                        except Exception:
                            pass
                if not abort_event.is_set():
                    await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)
        if abort_event.is_set():
            status = "Stopped"
    except asyncio.CancelledError:
        status = "Cancelled (/killall)"
        raise
    except FloodWait as exc:
        status = "Stopped (FloodWait)"
        abort_event.set()
        await notify(message, f"⏳ Telegram requires `{exc.value}s` before retrying. Batch stopped; no repeated requests.")
    except AuthKeyDuplicated:
        status = "Stopped (session conflict)"
        abort_event.set()
        await notify(message, "❌ Session conflict: use one instance and a valid new SESSION_STRING.")
    except Exception as exc:
        status = "Stopped (error)"
        await notify(message, f"❌ Board-first batch failed: `{exc}`")
    finally:
        if loading:
            try:
                await loading.delete()
            except Exception:
                pass
        try:
            await message.reply(f"🎞 **Board-first Batch: {status}**\n"
                                f"✅ Sent: `{processed}` • ⏭ Skipped: `{skipped}` • ❌ Failed: `{failed}`")
        except Exception:
            pass
