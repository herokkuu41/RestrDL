"""Preserve source pin flags and map them to delivered message IDs."""
from pyrogram import raw, utils
from pyrogram.errors import FloodWait, FloodPremiumWait

from config import PyroConf
from logger import LOGGER


async def source_messages(client, chat_id, message_ids):
    # Pyrofork 2.3.69 drops raw Message.pinned during high-level parsing.
    # Read and parse the same RPC ourselves, preserving that flag without an
    # extra metadata request for every video or batch page.
    if not PyroConf.MIRROR_SOURCE_PINS or not callable(getattr(client, "invoke", None)):
        return await client.get_messages(chat_id=chat_id, message_ids=message_ids)
    single = isinstance(message_ids, int)
    ids = [message_ids] if single else list(message_ids)
    peer = await client.resolve_peer(chat_id)
    inputs = [raw.types.InputMessageID(id=i) for i in ids]
    query = (raw.functions.channels.GetMessages(channel=peer, id=inputs)
             if isinstance(peer, raw.types.InputPeerChannel)
             else raw.functions.messages.GetMessages(id=inputs))
    result = await client.invoke(query, sleep_threshold=30)
    messages = await utils.parse_messages(client, result, replies=0)
    pinned = {m.id for m in result.messages if getattr(m, "pinned", False)}
    for message in messages:
        message._source_is_pinned = message.id in pinned
    return (messages[0] if messages else None) if single else messages


async def source_album(client, chat_id, message_id):
    messages = sorted(await client.get_media_group(chat_id, message_id), key=lambda m: m.id)
    if PyroConf.MIRROR_SOURCE_PINS and any(not hasattr(m, "_source_is_pinned") and
            not hasattr(m, "is_pinned") for m in messages):
        fresh = await source_messages(client, chat_id, [m.id for m in messages])
        flags = {m.id: is_source_pinned(m) for m in fresh}
        for message in messages:
            message._source_is_pinned = flags.get(message.id, False)
    return messages


def is_source_pinned(message):
    return bool(getattr(message, "_source_is_pinned", False) or getattr(message, "is_pinned", False))


def attach_source_pairs(sources, sent):
    if not sent:
        return None
    sent = sorted(sent, key=lambda m: m.id)
    sources = sorted(sources, key=lambda m: m.id)
    first = sent[0]
    # Never guess a destination album member when Telegram returned fewer IDs.
    first._source_pin_pairs = list(zip(sources, sent)) if len(sources) == len(sent) else []
    return first


async def mirror_source_pins(bot, user, source, sent, sender, bot_chat, user_chat, notify=None):
    if not PyroConf.MIRROR_SOURCE_PINS:
        return set()
    pairs = getattr(sent, "_source_pin_pairs", [(source, sent)])
    private = bot_chat != user_chat
    primary = user if private and sender == "user" else bot
    target = user_chat if primary is user and private else bot_chat
    mirrored = set()
    failure = None
    for original, delivered in pairs:
        if not is_source_pinned(original):
            continue
        attempts = [(primary, target, private)]
        if private:
            attempts.append((primary, target, False))
        elif primary is bot:
            attempts.append((user, bot_chat, False))
        for client, chat, both_sides in attempts:
            try:
                await client.pin_chat_message(chat, delivered.id,
                    disable_notification=True, both_sides=both_sides)
                mirrored.add(delivered.id)
                LOGGER(__name__).info("Mirrored source pin: source=%s destination=%s message=%s sender=%s",
                                      original.id, chat, delivered.id, sender)
                break
            except (FloodWait, FloodPremiumWait) as error:
                failure = f"Telegram requires a {error.value}s wait before pinning"
                break
            except Exception as error:
                failure = str(error)[:200]
        if delivered.id not in mirrored and failure and notify:
            try:
                await notify("⚠️ **Delivered successfully, but could not mirror a source pin.**\n"
                             "Check the destination account's pin permissions.\n"
                             f"Telegram: `{failure}`")
            except Exception:
                LOGGER(__name__).warning("Could not report source pin failure: %s", failure)
    return mirrored
