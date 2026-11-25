"""PublicOutputGuard — a request middleware that makes public group output structurally impossible.

Every Bot API call passes through this middleware. Any call that targets a group/supergroup/channel
(chat_id < 0 or an @username) is allowed only if it is
  * read-only (getChatMember, getChat, getFile, ...), or
  * an ephemeral send (has ephemeral_message_parameters with a receiver), or
  * an ephemeral edit/delete (editEphemeralMessage*, deleteEphemeralMessage), or
  * leaving the chat.
Everything else — public sendMessage, replies, reactions, chat actions ("typing"), pins, edits,
deletions, forwards, copies — raises PublicOutputBlocked before any network I/O happens.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType

log = logging.getLogger(__name__)

READ_ONLY = frozenset({
    "GetChat", "GetChatMember", "GetChatAdministrators", "GetChatMemberCount", "GetFile", "GetMe",
    "GetUpdates", "GetWebhookInfo", "GetMyCommands", "GetForumTopicIconStickers", "GetStickerSet",
    "GetCustomEmojiStickers", "GetUserChatBoosts", "GetMyName", "GetMyDescription", "GetMyShortDescription",
    "GetChatMenuButton", "GetMyDefaultAdministratorRights", "LeaveChat",
})
EPHEMERAL_EDITS = frozenset({
    "EditEphemeralMessageText", "EditEphemeralMessageCaption", "EditEphemeralMessageMedia",
    "EditEphemeralMessageReplyMarkup", "DeleteEphemeralMessage",
})
CHAT_FIELDS = ("chat_id", "from_chat_id")


class PublicOutputBlocked(RuntimeError):
    pass


def _targets_group(value: Any) -> bool:
    if isinstance(value, int):
        return value < 0
    if isinstance(value, str):
        return value.startswith("@") or (value.lstrip("-").isdigit() and value.startswith("-"))
    return False


def check_method(method: TelegramMethod[Any]) -> None:
    name = type(method).__name__
    if name in READ_ONLY or name in EPHEMERAL_EDITS:
        return
    if name == "SetMyCommands":
        scope = getattr(method, "scope", None)
        kind = getattr(scope, "type", "default")
        if kind == "all_private_chats":
            return
        if kind == "chat" and not _targets_group(getattr(scope, "chat_id", None)):
            return
        if (kind == "chat_member" and getattr(scope, "user_id", 0) > 0
                and all(getattr(c, "is_ephemeral", False) for c in getattr(method, "commands", []))):
            return
        raise PublicOutputBlocked("SetMyCommands would expose a group command menu")
    targets = [getattr(method, f, None) for f in CHAT_FIELDS]
    if not any(_targets_group(t) for t in targets):
        return  # private chats (positive ids), inline answers, callback answers, setMyCommands, ...
    eph = getattr(method, "ephemeral_message_parameters", None)
    if eph is not None and getattr(eph, "receiver_user_id", None):
        # ephemeral sends may not forward/copy from a group into the group publicly
        if getattr(method, "from_chat_id", None) is not None:
            raise PublicOutputBlocked(f"{name} with from_chat_id is not allowed in groups")
        return
    raise PublicOutputBlocked(f"{name} would be visible in a group; blocked by PublicOutputGuard")


class PublicOutputGuard(BaseRequestMiddleware):
    def __init__(self) -> None:
        self.blocked = 0

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Any,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        try:
            check_method(method)
        except PublicOutputBlocked:
            self.blocked += 1
            log.error("public output blocked", extra={"method": type(method).__name__})
            raise
        return await make_request(bot, method)
