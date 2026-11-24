"""PublicOutputGuard: public group output must be impossible, even if code tries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import (
    CopyMessage,
    DeleteMessage,
    EditEphemeralMessageText,
    EditMessageText,
    ForwardMessage,
    GetChatMember,
    GetFile,
    PinChatMessage,
    SendChatAction,
    SendMessage,
    SendSticker,
    SetMessageReaction,
)
from aiogram.types import EphemeralMessageParameters, ReactionTypeEmoji

from tests.conftest import GROUP_ID, FakeProvider, FakeSession, FakeTelegram
from tests.test_e2e_group import make_settings
from translator_bot.app import App
from translator_bot.guard import PublicOutputBlocked, check_method

EPH = EphemeralMessageParameters(receiver_user_id=5)

BLOCKED = [
    SendMessage(chat_id=GROUP_ID, text="public"),
    SendMessage(chat_id="@somegroup", text="public"),
    SendMessage(chat_id=str(GROUP_ID), text="public"),
    SendSticker(chat_id=GROUP_ID, sticker="x"),
    SendChatAction(chat_id=GROUP_ID, action="typing"),
    SetMessageReaction(chat_id=GROUP_ID, message_id=1, reaction=[ReactionTypeEmoji(emoji="👍")]),
    PinChatMessage(chat_id=GROUP_ID, message_id=1),
    EditMessageText(chat_id=GROUP_ID, message_id=1, text="x"),
    DeleteMessage(chat_id=GROUP_ID, message_id=1),
    ForwardMessage(chat_id=GROUP_ID, from_chat_id=GROUP_ID, message_id=1),
    CopyMessage(chat_id=GROUP_ID, from_chat_id=GROUP_ID, message_id=1),
    SendMessage(chat_id=GROUP_ID, text="x", ephemeral_message_parameters=EphemeralMessageParameters()),
]
ALLOWED = [
    SendMessage(chat_id=GROUP_ID, text="private", ephemeral_message_parameters=EPH),
    SendSticker(chat_id=GROUP_ID, sticker="x", ephemeral_message_parameters=EPH),
    EditEphemeralMessageText(chat_id=GROUP_ID, receiver_user_id=5, ephemeral_message_id=1, text="x"),
    GetChatMember(chat_id=GROUP_ID, user_id=5),
    GetFile(file_id="f"),
    SendMessage(chat_id=12345, text="private chat"),
]


@pytest.mark.parametrize("method", BLOCKED, ids=lambda m: type(m).__name__)
def test_blocked(method: Any) -> None:
    with pytest.raises(PublicOutputBlocked):
        check_method(method)


@pytest.mark.parametrize("method", ALLOWED, ids=lambda m: type(m).__name__)
def test_allowed(method: Any) -> None:
    check_method(method)


async def test_guard_is_in_the_live_request_chain(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram()
    app = App(make_settings(tmp_path), session=FakeSession(tg), providers={"gemini": FakeProvider("gemini")},
              detector=detector)
    await app.start()
    try:
        before = len(tg.calls)
        with pytest.raises(PublicOutputBlocked):
            await app.bot.send_message(GROUP_ID, "this must never reach Telegram")
        with pytest.raises(PublicOutputBlocked):
            await app.bot.send_chat_action(GROUP_ID, "typing")
        assert len(tg.calls) == before  # blocked before any request was made
        assert app.guard.blocked == 2
    finally:
        await app.stop()


@pytest.mark.parametrize("scope", [None, {"type": "all_group_chats"}, {"type": "all_chat_administrators"},
                                  {"type": "chat", "chat_id": GROUP_ID},
                                  {"type": "chat_administrators", "chat_id": GROUP_ID}])
def test_group_command_menus_are_blocked(scope: Any) -> None:
    from aiogram.methods import SetMyCommands

    with pytest.raises(PublicOutputBlocked):
        check_method(SetMyCommands(commands=[{"command": "tr", "description": "Translate", "is_ephemeral": True}],
                                   scope=scope))


@pytest.mark.parametrize("ephemeral", [False, True])
def test_per_member_commands_must_be_ephemeral(ephemeral: bool) -> None:
    from aiogram.methods import SetMyCommands

    method = SetMyCommands(commands=[{"command": "tr", "description": "Translate", "is_ephemeral": ephemeral}],
                           scope={"type": "chat_member", "chat_id": GROUP_ID, "user_id": 123})
    if ephemeral:
        check_method(method)
    else:
        with pytest.raises(PublicOutputBlocked):
            check_method(method)
