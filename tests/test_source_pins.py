from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from pyrogram import raw

import main
from helpers import source_pins
import test_pin_feature as fake


@pytest.mark.parametrize("channel,protected", [(False, False), (True, False), (False, True), (True, True)])
async def test_source_pin_is_mirrored_after_copy_or_reupload(channel, protected):
    fake.reset(main.bot, main.user)
    fake.SOURCE_PINS.add(100)
    fake.PROTECTED_SOURCE[0] = protected
    if channel:
        main.DESTINATION_CHAT_ID = fake.CHANNEL_ID
    result = await main.handle_download(main.bot, fake.IncomingMessage("link"), "https://t.me/testchan/100")
    assert result["status"] == "success" and result["source_pin_first"]
    assert len(fake.PIN_LOG) == 1
    role, chat, mid, box, both = fake.PIN_LOG[0]
    assert mid == result["sent_msg_id"]
    assert role == ("user" if not protected and not channel else "bot")
    assert box != "saved"
    assert both == (not channel)
    await fake.drain_tasks()


async def test_only_the_pinned_album_member_is_pinned():
    fake.reset(main.bot, main.user)
    for mid in (100, 101, 102):
        fake.GROUP_MEMBERS[mid] = "album"
    fake.SOURCE_PINS.add(101)
    result = await main.handle_download(main.bot, fake.IncomingMessage("link"), "https://t.me/testchan/100")
    assert result["status"] == "success"
    assert fake.PIN_LOG[0][0:3] == ("user", fake.BOT_USERNAME, 9101)
    assert not result["source_pin_first"]
    await fake.drain_tasks()


async def test_reuploaded_album_preserves_pinned_member_mapping(monkeypatch):
    from helpers import utils
    fake.reset(main.bot, main.user)
    fake.PROTECTED_SOURCE[0] = True
    for mid in (100, 101, 102):
        fake.GROUP_MEMBERS[mid] = "album"
    fake.SOURCE_PINS.update((101, 102))
    async def relay(source, bot, messages, target, **kwargs):
        return [bot._deliver(target, source_id=m.id) for m in messages]
    monkeypatch.setattr(utils, "relay_album", relay)
    result = await main.handle_download(main.bot, fake.IncomingMessage("link"), "https://t.me/testchan/100")
    assert result["status"] == "success"
    assert [pin[2] for pin in fake.PIN_LOG] == [9101, 9102]
    assert all(pin[0] == "bot" for pin in fake.PIN_LOG)
    await fake.drain_tasks()


async def test_pin_permissions_do_not_fail_or_repeat_delivered_video():
    fake.reset(main.bot, main.user)
    main.DESTINATION_CHAT_ID = fake.CHANNEL_ID
    fake.SOURCE_PINS.add(100)
    main.bot.pin_error = main.user.pin_error = RuntimeError("CHAT_ADMIN_REQUIRED")
    result = await main.handle_download(main.bot, fake.IncomingMessage("link"), "https://t.me/testchan/100")
    assert result["status"] == "success" and not result["source_pin_first"]
    assert len(main.user.forwarded) == 1
    assert fake.bot_said("Delivered successfully")
    await fake.drain_tasks()


async def test_pin_first_no_does_not_disable_source_pin_mirroring():
    fake.reset(main.bot, main.user)
    fake.SOURCE_PINS.add(101)
    runner = await fake.start_prompt(main.bot, main.user, count=3)
    await main.pin_decision_callback(main.bot, fake.CallbackQuery(f"pin_decision:no:{fake.USER_ID}"))
    await runner
    assert [pin[2] for pin in fake.PIN_LOG] == [9101]
    await fake.drain_tasks()


async def test_pin_first_yes_does_not_duplicate_source_pin():
    fake.reset(main.bot, main.user)
    fake.SOURCE_PINS.add(100)
    runner = await fake.start_prompt(main.bot, main.user, count=2)
    await main.pin_decision_callback(main.bot, fake.CallbackQuery(f"pin_decision:yes:{fake.USER_ID}"))
    await runner
    assert [pin[2] for pin in fake.PIN_LOG] == [9100]
    await fake.drain_tasks()


async def test_raw_read_preserves_flag_dropped_by_library_parser(monkeypatch):
    peer = raw.types.InputPeerChannel(channel_id=123, access_hash=1)
    result = NS(messages=[NS(id=101, pinned=True), NS(id=102, pinned=False)])
    client = NS(resolve_peer=AsyncMock(return_value=peer), invoke=AsyncMock(return_value=result))
    monkeypatch.setattr(source_pins.utils, "parse_messages", AsyncMock(return_value=[NS(id=101), NS(id=102)]))
    parsed = await source_pins.source_messages(client, -100123, [101, 102])
    assert source_pins.is_source_pinned(parsed[0]) and not source_pins.is_source_pinned(parsed[1])
    query = client.invoke.call_args.args[0]
    assert isinstance(query, raw.functions.channels.GetMessages)
    assert [item.id for item in query.id] == [101, 102]
    client.invoke.assert_awaited_once()


async def test_mismatched_album_results_never_pin_an_unrelated_member():
    first = source_pins.attach_source_pairs([NS(id=1, is_pinned=True), NS(id=2)], [NS(id=900)])
    bot = NS(pin_chat_message=AsyncMock())
    result = await source_pins.mirror_source_pins(bot, NS(), NS(id=1), first, "bot", 1, 1)
    assert not result
    bot.pin_chat_message.assert_not_awaited()


async def test_source_flag_survives_real_pinned_runtime_parser():
    response = raw.types.messages.Messages(messages=[raw.types.Message(
        id=101, peer_id=raw.types.PeerUser(user_id=123), date=1700000000,
        message="Pinned video caption", pinned=True, entities=[])],
        chats=[], users=[raw.types.User(id=123, first_name="Source", usernames=[], restriction_reason=[])], topics=[])
    client = NS(message_cache={}, me=NS(id=42),
        resolve_peer=AsyncMock(return_value=raw.types.InputPeerUser(user_id=123, access_hash=1)),
        invoke=AsyncMock(return_value=response))
    parsed = await source_pins.source_messages(client, 123, 101)
    assert parsed.id == 101 and source_pins.is_source_pinned(parsed)


async def test_source_mirroring_can_be_disabled(monkeypatch):
    fake.reset(main.bot, main.user)
    fake.SOURCE_PINS.add(100)
    monkeypatch.setattr(main.PyroConf, "MIRROR_SOURCE_PINS", False)
    result = await main.handle_download(main.bot, fake.IncomingMessage("link"), "https://t.me/testchan/100")
    assert result["status"] == "success" and not fake.PIN_LOG
    await fake.drain_tasks()


async def test_batch_remembers_a_source_that_rejects_copying(monkeypatch):
    fake.reset(main.bot, main.user)
    forward = AsyncMock(side_effect=RuntimeError("CHAT_FORWARDS_RESTRICTED"))
    monkeypatch.setattr(main.user, "forward_messages", forward)
    notice = {"sent": False}
    for mid in (100, 101):
        result = await main.handle_download(main.bot, fake.IncomingMessage("link"),
            f"https://t.me/testchan/{mid}", clone_notice=notice)
        assert result["status"] == "success"
    assert forward.await_count == 1 and notice["protected_source"] == "testchan"
    assert fake.CHAT_MESSAGE_DOWNLOADS == [100, 101]
    await fake.drain_tasks()
