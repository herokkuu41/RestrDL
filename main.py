import os
import shutil
import psutil
import asyncio
from time import time
from aiohttp import web

from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, FloodWait, FloodPremiumWait, AuthKeyDuplicated
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from helpers.utils import (
    processMediaGroup,
    refresh_progress_message
)

from helpers.files import (
    fileSizeLimit,
    get_readable_file_size,
    get_readable_time
)

from helpers.msg import (
    getChatMsgID,
    get_parsed_msg
)
from helpers.transfer import relay_media, close_transfers, copy_album
from helpers.speedtest import run_speedtest, format_speedtest

from config import PyroConf
from logger import LOGGER

# Initialize the bot client
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=8,
    parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=PyroConf.MAX_CONCURRENT_TRANSMISSIONS,
    sleep_threshold=30,
)

# Client for user session
user = Client(
    "user_session",
    workers=8,
    session_string=PyroConf.SESSION_STRING,
    max_concurrent_transmissions=PyroConf.MAX_CONCURRENT_TRANSMISSIONS,
    sleep_threshold=30,
)

RUNNING_TASKS = set()
download_semaphore = None
BATCH_STATES = {}  

PIN_PROMPTS = {}

# How long the user has to answer the "pin the first post?" prompt.
PIN_PROMPT_TIMEOUT = PyroConf.PIN_PROMPT_TIMEOUT

# "seconds left" values shown while the prompt counts down.
PIN_COUNTDOWN_MARKS = (45, 30, 15, 5)

PIN_YES_ANSWERS = {"yes", "y", "yeah", "yep", "ya", "pin", "1", "✅", "👍"}
PIN_NO_ANSWERS = {"no", "n", "nope", "nah", "skip", "0", "❌", "👎"}


def new_pin_decision() -> dict:
    """Mutable pin state shared by the prompt, the callback and the batch loop."""
    return {
        "pin_first": False,
        "bot": None,
        "chat_id": None,
        "user_chat_id": None,
        "private": False,
        "first_msg_id": None,
        "first_msg": None,
        "first_sender": None,
        "pinned_in": None,
        "pinned": False,
        "pinning": False,
        "batch_started": False,
    }


def build_pin_prompt_text(seconds_left: int) -> str:
    return (
        "📌 **Pin the first post of this batch?**\n\n"
        "Say **Yes** and I will pin the **first post** of this batch to the top of the "
        "destination chat as soon as it is uploaded.\n\n"
        f"⏳ No answer in **{seconds_left}s** → the batch continues **without pinning**.\n"
        "Tap a button below, or simply reply `yes` / `no`."
    )


def build_pin_status_text(pin_first: bool) -> str:
    if pin_first:
        return "✅ **Pin enabled** — I will pin the first post of this batch."
    return "➡️ **No pinning** for this batch."


def pin_answer_alert(pin_first: bool) -> str:
    return "Pin enabled for this batch." if pin_first else "No pinning for this batch."


def pin_target(decision: dict):
    """(client, chat_id) that can pin the batch's first post.

    In a private chat every account has its OWN message ids.  A post the user
    session copied into the bot chat has an id only the user session knows, so
    it must be pinned by the user session (for both sides).  In a channel the
    ids are shared and the bot, an admin there, pins.
    """
    if decision.get("private") and decision.get("first_sender") == "user":
        return user, decision.get("user_chat_id")
    return decision.get("bot"), decision.get("chat_id")


def parse_pin_answer(text: str):
    """Return True/False when the text clearly answers the pin prompt, else None."""
    if not text:
        return None
    answer = text.strip().lower()
    if answer in PIN_YES_ANSWERS:
        return True
    if answer in PIN_NO_ANSWERS:
        return False
    return None


async def pin_first_post(decision: dict, notify=None, error_notify=None) -> bool:
    """Pin the first uploaded post of the batch (at most once)."""
    if decision.get("pinned"):
        return True
    if decision.get("pinning"):
        return False

    msg_id = getattr(decision.get("first_msg"), "id", None) or decision.get("first_msg_id")
    primary_client, primary_chat_id = pin_target(decision)
    if not msg_id or primary_chat_id is None or primary_client is None:
        return False

    is_private = bool(decision.get("private"))
    LOGGER(__name__).info(f"Pinning first batch post: primary_chat={primary_chat_id} message={msg_id}")
    decision["pinning"] = True

    # Build prioritized list of pin attempts: (client, chat_id, both_sides)
    attempts = []
    # 1. Primary target with both_sides (as configured)
    attempts.append((primary_client, primary_chat_id, is_private))
    # 2. Primary target with both_sides=False (Telegram allows single-side pin in bot chats)
    if is_private:
        attempts.append((primary_client, primary_chat_id, False))
        # Private IDs belong to the sender's message box. Reusing the number
        # with another client can pin an unrelated message with the same ID.
    else:
        # Channel/group fallback: if bot failed, try user session if available
        if primary_client == decision.get("bot") and user:
            attempts.append((user, primary_chat_id, False))

    pin_success = False
    last_err = None
    pinned_client = primary_client
    pinned_chat = primary_chat_id
    pinned_mid = msg_id

    seen_attempts = set()
    for client, chat_id, both_sides in attempts:
        attempt_key = (id(client), chat_id, both_sides)
        if attempt_key in seen_attempts:
            continue
        seen_attempts.add(attempt_key)

        try:
            try:
                existing = await client.get_messages(chat_id=chat_id, message_ids=msg_id)
            except Exception as lookup_error:
                LOGGER(__name__).info(f"Pre-pin lookup {msg_id} in {chat_id} failed: {lookup_error}")
                existing = None

            if existing is not None and getattr(existing, "empty", False):
                raise Exception(
                    f"[400 MESSAGE_ID_INVALID] message {msg_id} is not in chat {chat_id} - "
                    "the first post of this batch was not delivered there"
                )

            await client.pin_chat_message(
                chat_id, msg_id, disable_notification=True, both_sides=both_sides
            )
            pin_success = True
            pinned_client = client
            pinned_chat = chat_id
            pinned_mid = msg_id
            LOGGER(__name__).info(f"Pinned first post of batch: chat={chat_id} message={msg_id} (both_sides={both_sides})")
            break
        except Exception as e:
            last_err = e
            LOGGER(__name__).warning(f"Could not pin message {msg_id} in chat {chat_id} (both_sides={both_sides}): {e}")

    if pin_success:
        decision["pinning"] = False
        decision["pinned"] = True
        decision["pinned_in"] = (pinned_client, pinned_chat, pinned_mid)
        if notify:
            try:
                await notify("📌 **Pinned** the first post of this batch.")
            except Exception:
                pass
        return True
    else:
        decision["pinning"] = False
        decision["pin_failed"] = True
        send = error_notify or notify
        if send:
            try:
                await send(
                    "⚠️ **Could not pin the first post of this batch.**\n"
                    f"**Telegram said:** `{last_err}`\n\n"
                    "• For a channel/group destination the bot must be an **admin** there with "
                    "the **Pin messages** permission."
                )
            except Exception:
                pass
        return False


async def unpin_first_post(decision: dict, notify=None) -> bool:
    """Undo the pin when the user changes their mind while the batch is running."""
    if not decision.get("pinned"):
        return False
    client, chat_id, msg_id = decision.get("pinned_in") or (
        decision.get("bot"), decision.get("chat_id"), decision.get("first_msg_id"))
    try:
        await client.unpin_chat_message(chat_id, msg_id)
    except Exception as e:
        LOGGER(__name__).warning(f"Could not unpin the first batch post: {e}")
        if decision.get("private"):
            alt_client = user if client == decision.get("bot") else decision.get("bot")
            alt_chat = decision.get("user_chat_id") if alt_client == user else decision.get("chat_id")
            if alt_client and alt_chat:
                try:
                    await alt_client.unpin_chat_message(alt_chat, msg_id)
                except Exception:
                    pass
        return False

    decision["pinned"] = False
    if notify:
        try:
            await notify("📌 **Unpinned** the first post of this batch.")
        except Exception:
            pass
    return True


async def apply_pin_decision(user_id: int, pin_first: bool, query=None) -> bool:
    """Record the user's choice and release the batch that waits on the prompt."""
    prompt = PIN_PROMPTS.get(user_id)
    if not prompt or prompt.get("cancelled"):
        return False

    pin_first = bool(pin_first)
    decision = prompt["decision"]
    first_decision = not prompt.get("decided")
    changed = prompt.get("current_choice") != pin_first

    prompt["current_choice"] = pin_first
    decision["pin_first"] = pin_first

    if first_decision:
        prompt["decided"] = True
        event = prompt.get("event")
        if event and not event.is_set():
            event.set()

    if (first_decision or changed) and pin_first and decision.get("batch_started") \
            and (decision.get("first_msg") or decision.get("first_msg_id")) \
            and not decision.get("pinned"):
        # Answered late, while the batch is already uploading: pin right away.
        # An explicit tap may retry after an earlier automatic failure.
        decision["pin_failed"] = False
        await pin_first_post(decision, notify=prompt.get("notify"), error_notify=prompt.get("error_notify"))
    elif (first_decision or changed) and not pin_first and decision.get("pinned"):
        await unpin_first_post(decision, notify=prompt.get("notify"))

    if first_decision or changed:
        try:
            await prompt["prompt_msg"].edit(
                build_pin_status_text(pin_first),
                reply_markup=prompt.get("markup")
            )
        except Exception:
            pass

    if query is not None:
        await query.answer(pin_answer_alert(pin_first))
    return True


async def release_pending_prompt(user_id: int, status_text: str = None, expect: dict = None) -> bool:
    """Close the pin prompt (dropping its buttons) and release any waiter."""
    prompt = PIN_PROMPTS.get(user_id)
    if not prompt or (expect is not None and prompt is not expect):
        return False

    PIN_PROMPTS.pop(user_id, None)
    prompt["decided"] = True
    prompt["cancelled"] = True

    event = prompt.get("event")
    if event and not event.is_set():
        event.set()

    if status_text:
        try:
            await prompt["prompt_msg"].edit(status_text)
        except Exception:
            pass
    return True


async def pin_prompt_countdown(user_id: int):
    """Tick the prompt text down; the hard deadline is enforced by the waiter."""
    marks = [m for m in PIN_COUNTDOWN_MARKS if 0 < m < PIN_PROMPT_TIMEOUT]
    previous = PIN_PROMPT_TIMEOUT
    for seconds_left in marks:
        await asyncio.sleep(max(previous - seconds_left, 0))
        previous = seconds_left
        prompt = PIN_PROMPTS.get(user_id)
        if not prompt or prompt.get("decided") or prompt.get("cancelled"):
            return
        try:
            await prompt["prompt_msg"].edit(
                build_pin_prompt_text(seconds_left),
                reply_markup=prompt.get("markup")
            )
        except Exception:
            return

# GLOBAL SETTING FOR DESTINATION CHANNEL
DESTINATION_CHAT_ID = None


async def resolve_destination(bot: Client, source_message: Message | None = None):
    """Where a job's output goes, resolved per client -> (bot_chat_id, user_chat_id).

    * A channel set with /set: both clients upload there.
    * No channel set: the private chat between the user and this bot.
      The bot client addresses it with the user id, but the USER client must
      address it with the BOT (id/username) - a user session sending to its own
      id would silently land in *Saved Messages* instead of this chat.
    """
    if DESTINATION_CHAT_ID:
        requester_id = getattr(getattr(source_message, "from_user", None), "id", None)
        if requester_id is not None and DESTINATION_CHAT_ID == requester_id:
            # A "destination" that is the requester's own id is Saved Messages for the
            # user client: copies land outside this chat and can never be pinned.
            LOGGER(__name__).warning(
                f"Ignoring /set destination {DESTINATION_CHAT_ID}: it is the requester's own "
                "user id (= Saved Messages for the user client). Using the bot chat instead."
            )
        else:
            return DESTINATION_CHAT_ID, DESTINATION_CHAT_ID
    if not bot.me:
        await bot.get_me()
    bot_chat_id = source_message.chat.id if source_message else bot.me.id
    user_chat_id = bot.me.username or bot.me.id
    return bot_chat_id, user_chat_id


async def resolve_target_chat_id(bot: Client, source_message: Message | None = None):
    """Id the BOT client uploads to (and the chat we pin in)."""
    bot_chat_id, _ = await resolve_destination(bot, source_message)
    return bot_chat_id


async def resolve_user_target_chat_id(bot: Client, source_message: Message | None = None):
    """Id the USER client uploads to (the bot chat when no channel is set)."""
    _, user_chat_id = await resolve_destination(bot, source_message)
    return user_chat_id

PROTECTED_REASON = (
    "content protection is enabled on the source chat - Telegram blocks forwarding "
    "and copying there, so the file has to be downloaded"
)


async def get_album_ids(chat_id, message_id):
    """Ids of every post in the album containing message_id (one API call)."""
    try:
        group = await user.get_media_group(chat_id, message_id)
        ids = sorted(m.id for m in group or [] if m and not getattr(m, "empty", False))
        if ids:
            return ids
    except FloodWait:
        raise
    except Exception as e:
        LOGGER(__name__).info(f"Could not read album of {message_id}: {e}")
    return [message_id]


async def try_clone(bot: Client, chat_message, chat_id, message_id,
                    target_chat_id, user_target_chat_id):
    """Clone a post without downloading it -> (sent_msg, strategy, sender, errors).

    Forwarding with drop_author=True is tried first: Telegram copies the post
    server-side (no author shown, captions and albums intact), with no file
    transfer and no caption re-parsing.  copy_* is the second choice, then the
    same two with the bot account.  Every failure reason is kept.
    """
    if getattr(chat_message, "has_protected_content", False):
        return None, None, None, [PROTECTED_REASON]

    errors = []
    is_group = bool(chat_message.media_group_id)
    ids = await get_album_ids(chat_id, message_id) if is_group else [message_id]

    plan = (
        ("user-forward", "user", user_target_chat_id),
        ("user-copy", "user", user_target_chat_id),
        ("bot-forward", "bot", target_chat_id),
        ("bot-copy", "bot", target_chat_id),
    )
    for name, sender, dest in plan:
        client = user if sender == "user" else bot
        try:
            if name.endswith("forward"):
                sent = await client.forward_messages(
                    chat_id=dest, from_chat_id=chat_id, message_ids=ids, drop_author=True)
            elif is_group:
                sent = await copy_album(client, dest, chat_id, message_id)
            else:
                sent = await client.copy_message(
                    chat_id=dest, from_chat_id=chat_id, message_id=message_id)

            if isinstance(sent, list):
                sent = sent[0] if sent else None
            if sent:
                LOGGER(__name__).info(f"Cloned {ids} via {name}")
                return sent, name, sender, errors
            errors.append(f"{name}: empty result")
        except FloodWait:
            raise
        except AuthKeyDuplicated:
            raise
        except Exception as e:
            if "AUTH_KEY_DUPLICATED" in str(e).upper():
                raise AuthKeyDuplicated(e)
            LOGGER(__name__).info(f"{name} failed for {ids}: {e}")
            errors.append(f"{name}: {e}")

    return None, None, None, errors


async def announce_clone_fallback(message, errors, notice=None):
    """Say why a post has to be downloaded - BEFORE the download starts."""
    if notice is not None:
        if notice.get("sent"):
            return
        notice["sent"] = True
    reason = errors[0] if errors else "unknown"
    try:
        await reply_temporary(message,
            "ℹ️ **Could not clone — downloading and re-uploading instead.**\n"
            f"**Reason:** `{reason}`")
    except Exception:
        pass


def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task


async def delete_later(msg, delay: float):
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except Exception:
        pass


CLEANUP_TASKS = set()      # NOT in RUNNING_TASKS: /killall must not cancel them
ACTIVE_BATCHES = {}        # user id -> {"abort": Event, "cancelled": bool}


def schedule_delete(msg, delay: float = None):
    """Self-destruct a temporary (error) message after ERROR_MESSAGE_TTL."""
    if msg is None:
        return None
    task = asyncio.create_task(
        delete_later(msg, PyroConf.ERROR_MESSAGE_TTL if delay is None else delay))
    CLEANUP_TASKS.add(task)
    task.add_done_callback(CLEANUP_TASKS.discard)
    return msg


async def reply_temporary(message, text, delay: float = None):
    """Show an error now, remove it again after ERROR_MESSAGE_TTL."""
    return schedule_delete(await message.reply(text), delay)


@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    welcome_text = (
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can grab photos, videos, audio, and documents from any Telegram post.\n"
        "Just send me a link (paste it directly or use `/dl <link>`),\n"
        "or reply to a message with `/dl`.\n\n"
        "⚡ **TRANSFER TOOLS**\n"
        "Use `/batch` to clone/download multiple messages easily!\n"
        "Use `/set <channel_id>` to set a custom upload destination.\n"
        "Use `/speedtest` to measure this server's download, upload and latency.\n"
        "Batch mode first asks whether to **pin the first post** — tap a button or reply `yes` / `no`.\n\n"
        "ℹ️ Use `/help` to view all commands and examples.\n"
        "🔒 Make sure the user client is part of the chat.\n\n"
        "Ready? Send me a Telegram post link!"
    )

    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(welcome_text, reply_markup=markup, disable_web_page_preview=True)


@bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Single Download**\n"
        "   – Just paste a link or use `/dl <link>`.\n\n"
        "➤ **Batch Process (Simple)**\n"
        "   1. Send `/batch`\n"
        "   2. Send the **Start Link**\n"
        "   3. Send the **Number of Messages** (e.g., 100)\n"
        "   4. Answer the **📌 Pin the first post?** prompt (button, or reply `yes` / `no`) —\n"
        f"      it auto-continues without pinning after {PyroConf.PIN_PROMPT_TIMEOUT}s.\n"
        "   The bot will calculate the range and process them.\n\n"
        "➤ **Destination Settings**\n"
        "   – `/set -100xxxx`: Set a channel for uploads.\n"
        "   – `/set none`: Reset to default (upload to the bot chat).\n"
        "     *Note: Bot must be admin in the target channel.*\n\n"
        "➤ **Requirements**\n"
        "   – Make sure the user client is part of the chat.\n\n"
        "➤ **Management**\n"
        "   – `/killall` : Cancel all running tasks.\n"
        "   – `/logs` : Get log file.\n"
        "   – `/stats` : System status.\n\n"
        "   – `/speedtest` : Server network download/upload test (run when idle).\n\n"
        "➤ **Notes**\n"
        "   – With no destination set, **everything** (text, photos, videos, files) is\n"
        "     delivered to **this bot chat** — never to Saved Messages.\n"
        f"   – Error messages delete themselves after {PyroConf.ERROR_MESSAGE_TTL // 60} min.\n"
    )
    
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)


@bot.on_message(filters.command("set") & filters.private)
async def set_destination(bot: Client, message: Message):
    global DESTINATION_CHAT_ID
    
    if len(message.command) < 2:
        await reply_temporary(message,
            "❌ **Usage:** `/set <channel_id>`\n"
            "Example: `/set -100123456789`\n"
            "To reset: `/set none`"
        )
        return

    input_arg = message.command[1]

    if input_arg.lower() == "none":
        DESTINATION_CHAT_ID = None
        await message.reply("✅ **Destination removed.** Files will now be stored in the bot chat.")
        return

    try:
        try:
            target_id = int(input_arg)
        except ValueError:
            chat_obj = await bot.get_chat(input_arg)
            target_id = chat_obj.id

        try:
            sent_msg = await bot.send_message(target_id, "✅ **Destination Channel Connected Successfully!**")
        except Exception as e:
            await reply_temporary(message,
                f"❌ **Failed to connect to channel `{target_id}`**.\n\n"
                f"**Error:** `{e}`\n"
                "👉 Make sure the Bot is an **Admin** in that channel with post permissions."
            )
            return

        DESTINATION_CHAT_ID = target_id
        await message.reply(f"✅ **Destination Channel Set!**\nAll downloads will now be uploaded to ID: `{target_id}`")
        LOGGER(__name__).info(f"Destination channel set to {target_id} by user {message.from_user.id}")

    except Exception as e:
        await reply_temporary(message, f"❌ **Error:** {str(e)}")


# -------------------------------------------------------------------------------------
# CORE DOWNLOAD LOGIC
# -------------------------------------------------------------------------------------
async def handle_download(bot: Client, message: Message, post_url: str, silent: bool = False, pre_fetched_msg=None, abort_event: asyncio.Event = None, clone_notice: dict = None):
    # If abort signal is triggered globally, exit instantly.
    if abort_event and abort_event.is_set():
        return "aborted"

    global download_semaphore
    if download_semaphore is None:
        download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)

    async with download_semaphore:
        if abort_event and abort_event.is_set():
            return "aborted"
            
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]

        target_chat_id = await resolve_target_chat_id(bot, message)
        user_target_chat_id = await resolve_user_target_chat_id(bot, message)
        progress_message = None

        try:
            chat_id, message_id, thread_id = getChatMsgID(post_url)
            
            if pre_fetched_msg:
                chat_message = pre_fetched_msg
            else:
                chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)
            
            LOGGER(__name__).info(f"Processing URL: {post_url}")
            sent_msg, clone_strategy, clone_sender, clone_errors = await try_clone(
                bot, chat_message, chat_id, message_id,
                target_chat_id, user_target_chat_id
            )
            if sent_msg:
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)
                return {
                    "status": "success",
                    "sent_msg": sent_msg,
                    "sent_msg_id": getattr(sent_msg, "id", None),
                    "sent_by": clone_sender,
                    "cloned": True,
                }
            LOGGER(__name__).info(f"Clone unavailable for {post_url}: {clone_errors}")
            await announce_clone_fallback(message, clone_errors, clone_notice)

            # --- FALLBACK: DOWNLOAD & UPLOAD ---
            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = (
                    chat_message.document.file_size if chat_message.document
                    else chat_message.video.file_size if chat_message.video
                    else chat_message.audio.file_size
                )
                if not await fileSizeLimit(file_size, message, "upload", getattr(bot.me, "is_premium", False)):
                    return "error"

            parsed_caption = await get_parsed_msg(chat_message.caption or "", chat_message.caption_entities)
            parsed_text = await get_parsed_msg(chat_message.text or "", chat_message.entities)

            if chat_message.media_group_id:
                sent_msg = await processMediaGroup(chat_message, bot, message, destination_chat_id=target_chat_id,
                                                  source_client=user, abort_event=abort_event)
                if not sent_msg:
                    if not silent:
                        await reply_temporary(message, "**Could not extract any valid media from the media group.**")
                    return "error"
                return {
                    "status": "success",
                    "sent_msg": sent_msg,
                    "sent_msg_id": getattr(sent_msg, "id", None),
                    "sent_by": "bot",
                    "clone_errors": clone_errors,
                }

            elif chat_message.media:
                if not silent:
                    progress_message = await message.reply("⚡ **PREPARING TRANSFER**\n\n📥 Download + 📤 upload\n🧠 Bounded memory stream • no full-file disk buffer")

                sent_msg = await relay_media(user, bot, chat_message, target_chat_id,
                                             progress_message=progress_message, abort_event=abort_event)
                if not sent_msg:
                    raise RuntimeError("Transfer did not return a sent message")
                
                if progress_message:
                    await progress_message.delete()
                    
                return {
                    "status": "success",
                    "sent_msg": sent_msg,
                    "sent_msg_id": getattr(sent_msg, "id", None),
                    "sent_by": "bot",
                    "clone_errors": clone_errors,
                }

            elif chat_message.text or chat_message.caption:
                sent_msg = await bot.send_message(target_chat_id, parsed_text or parsed_caption)
                return {
                    "status": "success",
                    "sent_msg": sent_msg,
                    "sent_msg_id": sent_msg.id,
                    "sent_by": "bot",
                    "clone_errors": clone_errors,
                }
            else:
                if not silent:
                    await reply_temporary(message, "**No media or text found in the post URL.**")
                return "error"

        # --- GLOBAL ERROR HANDLING & ABORT LOGIC ---
        except asyncio.CancelledError:
            if progress_message:
                try:
                    await progress_message.delete()
                except Exception:
                    pass
            raise

        except FloodPremiumWait as e:
            LOGGER(__name__).warning("Telegram account download throttle: FLOOD_PREMIUM_WAIT_%s", e.value)
            if abort_event:
                abort_event.set()
            await reply_temporary(message,
                f"⏳ **Telegram download limit**\n\nTelegram requires `{e.value}s` before another request.\n"
                "The source account's non-Premium download limit is active. More RAM or upload workers "
                "cannot remove this limit. The batch has stopped; wait before trying again.")
            if progress_message:
                await progress_message.delete()
            return "aborted"

        except FloodWait as e:
            if abort_event and not abort_event.is_set():
                abort_event.set() # Trigger global shut down
                await reply_temporary(message, f"🚨 **FloodWait Triggered!**\nTelegram requires a wait of `{e.value}` seconds. Process Aborted.")
            if progress_message:
                await progress_message.delete()
            return "aborted"

        except AuthKeyDuplicated as e:
            if abort_event and not abort_event.is_set():
                abort_event.set()
                await reply_temporary(
                    message,
                    "🚨 **Session Conflict (AUTH_KEY_DUPLICATED)!**\n\n"
                    "Telegram invalidated this session because the same `SESSION_STRING` was used in more than one place simultaneously.\n\n"
                    "**To fix this:**\n"
                    "1. Ensure the bot is only running in ONE place (stop local script or duplicate workload).\n"
                    "2. Generate a fresh session string via @SmartUtilBot.\n"
                    "3. Update `SESSION_STRING` in your environment and restart."
                )
            if progress_message:
                await progress_message.delete()
            return "aborted"
            
        except (PeerIdInvalid, KeyError):
            if abort_event and abort_event.is_set(): return "aborted"
            err = f"**Error processing {post_url}: User client likely not in chat.**"
            if not silent:
                if progress_message:
                    await progress_message.edit(err)
                    schedule_delete(progress_message)
                else:
                    await reply_temporary(message, err)
            return "error"
            
        except Exception as e:
            if "FLOOD_WAIT" in str(e).upper():
                if abort_event and not abort_event.is_set():
                    abort_event.set()
                    await reply_temporary(message, f"🚨 **FloodWait Triggered!**\nProcess Aborted.")
                if progress_message:
                    await progress_message.delete()
                return "aborted"

            if "AUTH_KEY_DUPLICATED" in str(e).upper():
                if abort_event and not abort_event.is_set():
                    abort_event.set()
                    await reply_temporary(
                        message,
                        "🚨 **Session Conflict (AUTH_KEY_DUPLICATED)!**\n\n"
                        "Telegram invalidated this session because the same `SESSION_STRING` was used in more than one place simultaneously.\n\n"
                        "**To fix this:**\n"
                        "1. Ensure the bot is only running in ONE place (stop local script or duplicate workload).\n"
                        "2. Generate a fresh session string via @SmartUtilBot.\n"
                        "3. Update `SESSION_STRING` in your environment and restart."
                    )
                if progress_message:
                    await progress_message.delete()
                return "aborted"
                
            if abort_event and abort_event.is_set(): return "aborted"
            
            error_message = f"**❌ Error at {post_url}: {str(e)}**"
            if not silent:
                if progress_message:
                    await progress_message.edit(error_message)
                    schedule_delete(progress_message)
                else:
                    await reply_temporary(message, error_message)
            LOGGER(__name__).error(e)
            return "error"


@bot.on_message(filters.command("dl") & filters.private)
async def download_media_cmd(bot: Client, message: Message):
    if len(message.command) < 2:
        await reply_temporary(message, "**Provide a post URL after the /dl command.**")
        return
    post_url = message.command[1]
    
    try:
        await track_task(handle_download(bot, message, post_url, silent=False))
    except FloodWait as e:
        await reply_temporary(message, f"🚨 **FloodWait Triggered!**\nTelegram requires a wait of `{e.value}` seconds.")
    except Exception as e:
        if "FLOOD_WAIT" in str(e).upper():
             await reply_temporary(message, f"🚨 **FloodWait Triggered!**")


# -------------------------------------------------------------------------------------
# NEW /BATCH INTERACTIVE FLOW
# -------------------------------------------------------------------------------------
@bot.on_message(filters.command("batch") & filters.private)
async def batch_command_start(bot: Client, message: Message):
    user_id = message.from_user.id
    # Never leave a previous prompt open: the batch waiting on it would hang.
    await release_pending_prompt(
        user_id,
        "⌛ A new /batch was started, so this pin prompt is closed."
    )
    BATCH_STATES[user_id] = {'step': 'ask_link'}
    await message.reply(
        "🚀 **Batch Mode Initiated**\n\n"
        "Please send the **Start Link** of the first post you want to download."
    )


@bot.on_message(filters.private & ~filters.command(["start", "help", "dl", "batch", "stats", "logs", "killall", "set", "speedtest"]))
async def handle_text_and_states(bot: Client, message: Message):
    user_id = message.from_user.id
    state = BATCH_STATES.get(user_id)

    if state:
        if state['step'] == 'ask_link':
            if not message.text.startswith("https://t.me/"):
                await reply_temporary(message, "❌ Invalid link. Please send a valid Telegram post link (e.g., https://t.me/channel/100).")
                return

            BATCH_STATES[user_id]['start_link'] = message.text
            BATCH_STATES[user_id]['step'] = 'ask_count'
            await message.reply(
                "✅ Link accepted.\n\n"
                "**How many messages** do you want to process starting from there?\n"
                "(Send a number, e.g., `100`)"
            )
            return

        elif state['step'] == 'ask_count':
            if not message.text.isdigit():
                await reply_temporary(message, "❌ Please send a valid number.")
                return

            count = int(message.text)
            start_link = BATCH_STATES[user_id]['start_link']

            del BATCH_STATES[user_id]

            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes, pin it", callback_data=f"pin_decision:yes:{user_id}"),
                InlineKeyboardButton("❌ No", callback_data=f"pin_decision:no:{user_id}"),
            ]])
            prompt_msg = await message.reply(build_pin_prompt_text(PIN_PROMPT_TIMEOUT), reply_markup=markup)

            decision_event = asyncio.Event()
            pin_prompt = {
                "event": decision_event,
                "start_link": start_link,
                "count": count,
                "prompt_msg": prompt_msg,
                "markup": markup,
                "notify": message.reply,
                "error_notify": lambda text: reply_temporary(message, text),
                "decided": False,
                "cancelled": False,
                "current_choice": None,
                "decision": new_pin_decision(),
            }
            PIN_PROMPTS[user_id] = pin_prompt
            track_task(pin_prompt_countdown(user_id))

            # Wait for the answer, but ALWAYS continue afterwards: a cancelled countdown
            # task or a /killall must never leave the batch stuck on this prompt.
            try:
                await asyncio.wait_for(decision_event.wait(), timeout=PIN_PROMPT_TIMEOUT)
            except asyncio.TimeoutError:
                current = PIN_PROMPTS.get(user_id)
                if current is pin_prompt and not current.get("decided"):
                    current["decided"] = True
                    current["current_choice"] = False
                    current["decision"]["pin_first"] = False
                    try:
                        await current["prompt_msg"].edit(
                            f"⏱️ **No answer in {PIN_PROMPT_TIMEOUT}s** — starting the batch "
                            "**without pinning**.\n"
                            "Changed your mind? Tap **✅ Yes, pin it** while the batch is running, "
                            "or reply `yes`.",
                            reply_markup=current.get("markup")
                        )
                    except Exception:
                        pass

            if pin_prompt.get("cancelled"):
                LOGGER(__name__).info(f"Pin prompt for {user_id} was closed before the batch started.")
                return

            await execute_batch_logic(
                bot, message, start_link, count,
                pin_decision=pin_prompt["decision"],
                pin_prompt=pin_prompt
            )
            return

    if message.text:
        answer = parse_pin_answer(message.text)
        if answer is not None and user_id in PIN_PROMPTS:
            await apply_pin_decision(user_id, answer)
            return

    if message.text and not message.text.startswith("/"):
        # Ignore random text in private chat; only Telegram post links should trigger /dl-like behavior.
        if not message.text.startswith("https://t.me/"):
            LOGGER(__name__).info(f"Ignoring non-link private message in idle mode: {message.text[:80]}")
            return
        try:
            await track_task(handle_download(bot, message, message.text, silent=False))
        except FloodWait as e:
            await message.reply(f"🚨 **FloodWait Triggered!**\nWait `{e.value}` seconds.")


# Helper to run the batch loop (NOW HIGHLY OPTIMIZED WITH BULK FETCH)
async def execute_batch_logic(bot: Client, message: Message, start_link: str, count: int,
                              pin_decision: dict = None, pin_prompt: dict = None):
    """Run the batch loop.

    `pin_decision` is the mutable pin state owned by the /batch prompt, so a choice
    made *while* the batch is already running (or after the auto-continue) is honoured.
    """
    if pin_decision is None:
        pin_decision = new_pin_decision()

    try:
        start_chat, start_id, start_thread_id = getChatMsgID(start_link)
    except Exception as e:
        return await reply_temporary(message, f"**❌ Error parsing start link:\n{e}**")

    end_id = start_id + count - 1
    prefix = start_link.rsplit("/", 1)[0]

    thread_text = f"\n**Topic/Thread Filter Active**: ID `{start_thread_id}`" if start_thread_id else ""
    loading = await message.reply(
        f"📥 **Starting Batch Process**\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 **From:** `{start_id}`  →  **To:** `{end_id}`\n"
        f"🗂 **Total Range Checked:** `{count}` posts{thread_text}\n"
        f"⚡ Parallel download + upload • 📌 pin follows source order"
    )

    downloaded = skipped = failed = 0
    batch_tasks = []
    BATCH_SIZE = PyroConf.BATCH_SIZE

    abort_event = asyncio.Event() # Shared flag to shut everything down
    seen_groups = {}              # each album is handled once, not per member id
    clone_notice = {"sent": False}
    batch_state = {"abort": abort_event, "cancelled": False}
    ACTIVE_BATCHES[message.from_user.id] = batch_state
    cancelled = 0

    pin_decision["bot"] = bot
    pin_decision["chat_id"] = await resolve_target_chat_id(bot, message)
    pin_decision["user_chat_id"] = await resolve_user_target_chat_id(bot, message)
    pin_decision["private"] = pin_decision["user_chat_id"] != pin_decision["chat_id"]
    pin_decision["batch_started"] = True

    ordered_results = {}  # Insertion order follows the original source posts.

    async def record_and_pin_first(source_id, res):
        """Later fast text posts cannot steal the pin from earlier media."""
        if pin_decision.get("first_msg_id") or abort_event.is_set():
            return
        ordered_results[source_id] = res
        for candidate in ordered_results.values():
            if candidate is None:
                return  # An earlier source post is still transferring.
            if isinstance(candidate, dict) and candidate.get("status") == "success" and candidate.get("sent_msg_id"):
                res = candidate
                pin_decision["first_msg_id"] = res["sent_msg_id"]
                pin_decision["first_msg"] = res.get("sent_msg")
                pin_decision["first_sender"] = res.get("sent_by") or "bot"
                ordered_results.clear()
                if pin_decision.get("pin_first") and not pin_decision.get("pinned") \
                        and not pin_decision.get("pin_failed"):
                    await pin_first_post(pin_decision, notify=message.reply,
                                         error_notify=lambda text: reply_temporary(message, text))
                return

    async def consume_results(results):
        """Tally a finished chunk and pin the first uploaded post exactly once."""
        nonlocal downloaded, failed, cancelled
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                cancelled += 1          # stopped by /killall - not a failure
                continue
            status = result.get("status") if isinstance(result, dict) else result
            if status == "aborted" or abort_event.is_set():
                continue
            if status == "success":
                downloaded += 1
            else:
                failed += 1

        if pin_decision.get("pin_first") and not pin_decision.get("pinned") \
                and not pin_decision.get("pin_failed"):
            await pin_first_post(pin_decision, notify=message.reply,
                                 error_notify=lambda text: reply_temporary(message, text))

    all_message_ids = list(range(start_id, end_id + 1))
    chunk_size = 50 # Fetch 50 messages per API call (Instant skipping)

    for i in range(0, len(all_message_ids), chunk_size):
        if abort_event.is_set():
            break

        chunk = all_message_ids[i:i+chunk_size]

        try:
            # OPTIMIZATION: Fetch in bulk to save API rate limits!
            messages_batch = await user.get_messages(chat_id=start_chat, message_ids=chunk)
        except FloodWait as e:
            await reply_temporary(message, f"🚨 **Batch Halted: Read FloodWait Triggered!**\nWait `{e.value}` seconds.")
            abort_event.set()
            break
        except AuthKeyDuplicated:
            await reply_temporary(
                message,
                "🚨 **Session Conflict (AUTH_KEY_DUPLICATED)!**\n"
                "Telegram invalidated this session because the same `SESSION_STRING` was used elsewhere simultaneously.\n"
                "Please generate a new `SESSION_STRING` with @SmartUtilBot and ensure only one instance is running."
            )
            abort_event.set()
            break
        except Exception as e:
            if "AUTH_KEY_DUPLICATED" in str(e).upper():
                await reply_temporary(
                    message,
                    "🚨 **Session Conflict (AUTH_KEY_DUPLICATED)!**\n"
                    "Telegram invalidated this session because the same `SESSION_STRING` was used elsewhere simultaneously.\n"
                    "Please generate a new `SESSION_STRING` with @SmartUtilBot and ensure only one instance is running."
                )
                abort_event.set()
                break
            if "FLOOD_WAIT" in str(e).upper():
                 await reply_temporary(message, f"🚨 **Batch Halted: Read FloodWait Triggered!**")
                 abort_event.set()
                 break
            LOGGER(__name__).error(e)
            failed += len(chunk)
            await reply_temporary(message, f"**⚠️ Could not read {len(chunk)} message(s): {e}**")
            continue

        if getattr(messages_batch, "id", None) is not None:
             messages_batch = [messages_batch]

        for chat_msg in sorted(messages_batch, key=lambda m: getattr(m, "id", 0)):
            if abort_event.is_set():
                break

            if not chat_msg or getattr(chat_msg, 'empty', False):
                skipped += 1
                continue

            if start_thread_id:
                msg_thread = getattr(chat_msg, "message_thread_id", None)
                if msg_thread != start_thread_id:
                    skipped += 1
                    continue

            if chat_msg.media_group_id:
                if chat_msg.media_group_id in seen_groups:
                    skipped += 1        # already handled with its album's first post
                    continue
                seen_groups[chat_msg.media_group_id] = chat_msg.id

            has_media = bool(chat_msg.media_group_id or chat_msg.media)
            has_text  = bool(chat_msg.text or chat_msg.caption)
            if not (has_media or has_text):
                skipped += 1
                continue

            async def run_and_track_task(source_id, coro):
                try:
                    res = await coro
                except BaseException as error:
                    await record_and_pin_first(source_id, error)
                    raise
                await record_and_pin_first(source_id, res)
                return res

            url = f"{prefix}/{chat_msg.id}"
            if not pin_decision.get("first_msg_id"):
                ordered_results[chat_msg.id] = None
            task = track_task(run_and_track_task(chat_msg.id, handle_download(
                bot, message, url, 
                silent=False, 
                pre_fetched_msg=chat_msg, 
                abort_event=abort_event, # Pass the global abort flag
                clone_notice=clone_notice
            )))
            batch_tasks.append(task)

            if len(batch_tasks) >= BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                await consume_results(results)

                batch_tasks.clear()
                if not abort_event.is_set():
                    await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

    if batch_tasks and not abort_event.is_set():
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        await consume_results(results)

    ACTIVE_BATCHES.pop(message.from_user.id, None)
    try:
        await loading.delete()
    except Exception:
        pass

    if batch_state["cancelled"]:
        completion_text = "**🛑 Batch Process Cancelled (/killall)**"
    elif abort_event.is_set():
        completion_text = "**🛑 Batch Process Stopped (FloodWait)**"
    else:
        completion_text = "**✅ Batch Process Complete!**"

    # A "Yes" that arrived late (after the auto-continue) is still honoured here.
    if pin_decision.get("pin_first") and not pin_decision.get("pinned") \
            and not pin_decision.get("pin_failed"):
        await pin_first_post(pin_decision, notify=message.reply,
                             error_notify=lambda text: reply_temporary(message, text))

    if pin_prompt is not None:
        await release_pending_prompt(
            message.from_user.id,
            "📌 Pin prompt closed — the first post of this batch is pinned."
            if pin_decision.get("pinned")
            else "⌛ Pin prompt closed — nothing was pinned.",
            expect=pin_prompt
        )

    await message.reply(
        f"{completion_text}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Processed** : `{downloaded}`\n"
        f"⏭️ **Skipped** : `{skipped}`\n"
        f"❌ **Failed** : `{failed}`"
        + (f"\n🛑 **Cancelled** : `{cancelled}`" if cancelled else "")
    )

@bot.on_message(filters.command("stats") & filters.private)
async def stats(_, message: Message):
    currentTime = get_readable_time(time() - PyroConf.BOT_START_TIME)
    total, used, free = shutil.disk_usage(".")
    total = get_readable_file_size(total)
    used = get_readable_file_size(used)
    free = get_readable_file_size(free)
    sent = get_readable_file_size(psutil.net_io_counters().bytes_sent)
    recv = get_readable_file_size(psutil.net_io_counters().bytes_recv)
    
    stats_msg = (
        "**Bot Status**\n\n"
        f"**➜ Uptime:** `{currentTime}`\n"
        f"**➜ Disk Free:** `{free}`\n"
        f"**➜ Upload:** `{sent}`\n"
        f"**➜ Download:** `{recv}`"
    )
    await message.reply(stats_msg)


@bot.on_message(filters.command("logs") & filters.private)
async def logs(_, message: Message):
    if os.path.exists("logs.txt"):
        await message.reply_document(document="logs.txt", caption="**Logs**")
    else:
        await message.reply("**Not exists**")


SPEEDTEST_RUNNING = False
SPEEDTEST_LAST_RUN = 0.0


@bot.on_message(filters.command("speedtest") & filters.private)
async def speedtest_command(_, message: Message):
    global SPEEDTEST_RUNNING, SPEEDTEST_LAST_RUN
    if SPEEDTEST_RUNNING:
        return await message.reply("⏳ **Speed test already running.** Please wait for its result.")
    if ACTIVE_BATCHES or any(not task.done() for task in RUNNING_TASKS):
        return await message.reply("⏳ **Transfers are active.** Run `/speedtest` after they finish for a useful result.")
    remaining = 300 - (time() - SPEEDTEST_LAST_RUN)
    if remaining > 0:
        return await message.reply(f"⏳ **Speed test cooldown:** `{remaining:.0f}s` remaining.")
    SPEEDTEST_RUNNING = True
    SPEEDTEST_LAST_RUN = time()
    status = None
    try:
        status = await message.reply(
            "🌐 **SERVER SPEED TEST**\n\n📡 Measuring latency, download and upload…\n"
            "Cloudflare endpoint • up to 96 MiB traffic • no disk buffer\n"
            "⏱ Usually under a minute. Cancel with `/killall`.")
        result = await track_task(run_speedtest())
        await status.edit(format_speedtest(result))
        LOGGER(__name__).info("Server speed test: %s", result)
    except asyncio.CancelledError:
        if status:
            await status.edit("🛑 **Server speed test cancelled.**")
    except Exception as error:
        LOGGER(__name__).warning("Server speed test failed: %s", error)
        if status:
            await status.edit("⚠️ **Speed test failed.** The test endpoint may be unavailable; see `/logs`.")
    finally:
        SPEEDTEST_RUNNING = False


@bot.on_callback_query(filters.regex(r"^pin_decision:(yes|no):(\d+)$"))
async def pin_decision_callback(_, query):
    parts = (query.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await query.answer("Invalid selection.", show_alert=True)
        return

    choice, owner_id = parts[1], int(parts[2])

    if not query.from_user or query.from_user.id != owner_id:
        await query.answer("This prompt is not for you.", show_alert=True)
        return

    applied = await apply_pin_decision(owner_id, choice == "yes", query=query)
    if not applied:
        await query.answer("This pin prompt is already closed.", show_alert=True)

@bot.on_callback_query(filters.regex("^refresh_progress$"))
async def refresh_progress_callback(_, query):
    refreshed, remaining = await refresh_progress_message(query.message)
    if refreshed:
        await query.answer("Progress refreshed.")
    else:
        if remaining:
            await query.answer(f"Heyy!! Wait for {remaining} sec", show_alert=True)
        else:
            await query.answer("No active progress for this message.", show_alert=True)


@bot.on_message(filters.command("killall") & filters.private)
async def cancel_all_tasks(_, message):
    cancelled = 0
    user_id = message.from_user.id
    if user_id in BATCH_STATES:
        del BATCH_STATES[user_id]

    # Release a pending pin prompt, otherwise the batch waiting on it hangs forever.
    await release_pending_prompt(user_id, "🛑 Pin prompt cancelled (/killall).")

    # Stop running batch loops too, otherwise they keep scheduling new posts.
    for state in list(ACTIVE_BATCHES.values()):
        state["cancelled"] = True
        state["abort"].set()

    for task in list(RUNNING_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    await message.reply(f"**Cancelled {cancelled} running task(s).**")


async def initialize():
    from pyrogram.crypto import aes
    if aes.tgcrypto is None:
        raise RuntimeError("TgCrypto is required for fast transfers; install requirements.txt")
    global download_semaphore
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)
    LOGGER(__name__).info(
        "Transfer settings: connections=%s/%s, requests-per-connection=%s/%s, active=%s, payload-buffer=%s MiB",
        PyroConf.PARALLEL_DOWNLOAD_WORKERS, PyroConf.PARALLEL_UPLOAD_WORKERS,
        PyroConf.DOWNLOAD_REQUESTS_PER_CONNECTION, PyroConf.UPLOAD_REQUESTS_PER_CONNECTION,
        PyroConf.MAX_ACTIVE_TRANSFERS, PyroConf.TRANSFER_BUFFER_MIB)


# -------------------------------------------------------------------------------------
# Web Server (Optional health check endpoint for cloud platforms)
# -------------------------------------------------------------------------------------
async def web_server():
    port_env = os.getenv('PORT')
    port = int(port_env) if port_env else 8080
    try:
        async def handle(request):
            return web.Response(text="Bot is running!")

        app = web.Application()
        app.router.add_get('/', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        LOGGER(__name__).info(f"Web server started on port {port}")
    except Exception as e:
        LOGGER(__name__).warning(f"Web server could not be started on port {port}: {e}. Bot will continue as worker.")


# -------------------------------------------------------------------------------------
# MAIN EXECUTION
# -------------------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Bot Started!")
        loop = asyncio.get_event_loop()
        
        loop.run_until_complete(initialize())
        
        user.start()
        
        loop.run_until_complete(web_server())
        
        bot.run()
        
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        try:
            loop.run_until_complete(close_transfers())
        except Exception as err:
            LOGGER(__name__).warning("Transfer shutdown: %s", err)
        if getattr(user, "is_connected", False):
            try:
                user.stop()
            except Exception:
                pass
        LOGGER(__name__).info("Bot Stopped")
