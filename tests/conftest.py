"""Shared test helpers: fake Telegram session (records every API call), fake providers, message builders.

All tests in this suite are MOCKED (no network). Live tests are marked `live` and skipped by default.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import TelegramMethod
from aiogram.types import Message, Update

from translator_bot.detect import LanguageDetector
from translator_bot.providers.base import (
    ErrorKind,
    ProviderError,
    TranslationJob,
    TranslationResult,
)
from translator_bot.textutil import html_to_plain

BOT_ID = 7000000001
BOT_TOKEN = f"{BOT_ID}:AAH-test-token-abcdefghijklmnopqrstuv"
GROUP_ID = -1001234567890
OWNER = 111  # English recipient (owner)
HINDI = 222  # approved recipient using Hindi
OUTSIDER = 333  # unapproved member
SPANISH_SPEAKER = 444  # unapproved member who writes in Spanish

_ids = itertools.count(1000)


@pytest.fixture(scope="session")
def detector() -> LanguageDetector:
    return LanguageDetector()


# --- fake Telegram ------------------------------------------------------------------------------


@dataclass
class Call:
    method: str
    params: dict[str, Any]


@dataclass
class FakeTelegram:
    """Canned Bot API. Records calls. Behaviour knobs let tests simulate failures."""

    members: dict[int, set[int]] = field(default_factory=dict)  # chat -> member user ids
    admins: set[int] = field(default_factory=set)  # chats where the bot is admin
    calls: list[Call] = field(default_factory=list)
    fail_ephemeral_for: set[int] = field(default_factory=set)  # receiver ids whose ephemeral sends fail
    forbid_dm_for: set[int] = field(default_factory=set)
    eph_counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    files: dict[str, bytes] = field(default_factory=dict)  # file_id -> content

    def respond(self, method: TelegramMethod[Any]) -> Any:
        name = type(method).__name__
        params = method.model_dump(exclude_none=True)
        self.calls.append(Call(name, params))
        if name == "GetMe":
            return {"id": BOT_ID, "is_bot": True, "first_name": "Babel", "username": "babel_test_bot",
                    "can_join_groups": True, "can_read_all_group_messages": False, "supports_inline_queries": True}
        if name == "GetChatMember":
            chat_id, uid = params["chat_id"], params["user_id"]
            user = {"id": uid, "is_bot": uid == BOT_ID, "first_name": f"u{uid}"}
            if uid == BOT_ID:
                if chat_id in self.admins:
                    return {"status": "administrator", "user": user, "can_be_edited": False, "is_anonymous": False,
                            "can_manage_chat": False, "can_delete_messages": False, "can_manage_video_chats": False,
                            "can_restrict_members": False, "can_promote_members": False, "can_change_info": False,
                            "can_invite_users": False, "can_post_stories": False, "can_edit_stories": False,
                            "can_delete_stories": False}
                return {"status": "member", "user": user}
            return {"status": "member" if uid in self.members.get(chat_id, set()) else "left", "user": user}
        if name == "GetChat":
            return {"id": params["chat_id"], "type": "supergroup", "title": "Test Group", "accent_color_id": 0,
                    "max_reaction_count": 11, "accepted_gift_types": {"unlimited_gifts": False,
                                                                      "limited_gifts": False,
                                                                      "unique_gifts": False,
                                                                      "premium_subscription": False}}
        if name == "SendMessage":
            chat_id = params["chat_id"]
            eph = params.get("ephemeral_message_parameters")
            base = {"message_id": 0 if eph else next(_ids), "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "supergroup" if chat_id < 0 else "private"},
                    "text": params["text"], "from": {"id": BOT_ID, "is_bot": True, "first_name": "Babel"}}
            if eph:
                rid = eph["receiver_user_id"]
                if rid in self.fail_ephemeral_for:
                    raise TelegramBadRequest(method=method, message="Bad Request: ephemeral message can't be sent")
                base["ephemeral_message_id"] = next(self.eph_counter)
                base["receiver_user"] = {"id": rid, "is_bot": False, "first_name": f"u{rid}"}
            elif chat_id > 0 and chat_id in self.forbid_dm_for:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot can't initiate conversation")
            return base
        if name == "GetFile":
            fid = params["file_id"]
            return {"file_id": fid, "file_unique_id": f"u-{fid}", "file_size": len(self.files.get(fid, b"")),
                    "file_path": f"files/{fid}"}
        if name in ("EditMessageText", "EditMessageReplyMarkup"):
            return True
        if name == "EditEphemeralMessageText" and params["receiver_user_id"] in self.fail_ephemeral_for:
            raise TelegramBadRequest(method=method, message="Bad Request: ephemeral message can't be edited")
        if name == "SendInvoice":
            return {"message_id": next(_ids), "date": int(time.time()),
                    "chat": {"id": params["chat_id"], "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Babel"}}
        return True  # SetMyCommands, AnswerCallbackQuery, AnswerInlineQuery, EditEphemeralMessageText, ...

    # convenience views
    def public_group_output(self) -> list[Call]:
        """Any call that would be visible to group members. Must always be empty."""
        out = []
        for c in self.calls:
            chat = c.params.get("chat_id")
            if isinstance(chat, int) and chat < 0 and c.method.startswith(("Send", "Forward", "Copy", "Pin",
                                                                            "SetMessageReaction", "Edit", "Delete")):
                if "ephemeral_message_parameters" in c.params or "Ephemeral" in c.method:
                    continue
                out.append(c)
        return out

    def ephemeral_to(self, user_id: int) -> list[Call]:
        return [c for c in self.calls if c.method == "SendMessage"
                and c.params.get("ephemeral_message_parameters", {}).get("receiver_user_id") == user_id]

    def ephemeral_edits_to(self, user_id: int) -> list[Call]:
        return [c for c in self.calls if c.method == "EditEphemeralMessageText"
                and c.params.get("receiver_user_id") == user_id]

    def dms_to(self, user_id: int) -> list[Call]:
        return [c for c in self.calls if c.method == "SendMessage" and c.params.get("chat_id") == user_id]


class FakeSession(BaseSession):
    def __init__(self, tg: FakeTelegram) -> None:
        super().__init__()
        self.tg = tg

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        return _coerce(method, self.tg.respond(method), bot)

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        fid = url.rsplit("/", 1)[-1]
        yield self.tg.files.get(fid, b"")


def _coerce(method: TelegramMethod[Any], result: Any, bot: Bot) -> Any:
    from pydantic import TypeAdapter

    ret = method.__returning__
    value = TypeAdapter(ret).validate_python(result, context={"bot": bot})
    return value


# --- builders -----------------------------------------------------------------------------------


def user(uid: int, name: str | None = None, is_bot: bool = False) -> dict[str, Any]:
    return {"id": uid, "is_bot": is_bot, "first_name": name or f"User{uid}"}


def group_message(text: str | None = None, *, sender: int = OUTSIDER, name: str | None = None,
                  message_id: int | None = None, chat_id: int = GROUP_ID, reply_to: dict[str, Any] | None = None,
                  is_bot: bool = False, via_bot: int | None = None, caption: str | None = None,
                  extra: dict[str, Any] | None = None, edit_date: int | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {
        "message_id": message_id or next(_ids), "date": int(time.time()),
        "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
        "from": user(sender, name, is_bot),
    }
    if text is not None:
        m["text"] = text
    if caption is not None:
        m["caption"] = caption
    if reply_to is not None:
        m["reply_to_message"] = reply_to
    if via_bot is not None:
        m["via_bot"] = user(via_bot, "Bot", True)
    if edit_date is not None:
        m["edit_date"] = edit_date
    if extra:
        m.update(extra)
    return m


def to_message(d: dict[str, Any], bot: Bot | None = None) -> Message:
    return Message.model_validate(d, context={"bot": bot} if bot else None)


def update(message: dict[str, Any], *, edited: bool = False) -> Update:
    return Update.model_validate({"update_id": next(_ids), ("edited_message" if edited else "message"): message})


# --- fake providers -----------------------------------------------------------------------------

# Deterministic "translations" for tests: (text, target) -> translation
DICTIONARY: dict[tuple[str, str], str] = {
    ("¿Alguien sabe si mañana estará abierto? 😂", "en"): "Does anyone know if it will be open tomorrow? 😂",
    ("¿Alguien sabe si mañana estará abierto? 😂", "hi"): "क्या किसी को पता है कि कल यह खुला रहेगा? 😂",
    ("Are you coming tonight?", "hi"): "क्या तुम आज रात आ रहे हो?",
    ("Que locura jajaja 😂", "en"): "That's crazy hahaha 😂",
    ("Que locura jajaja 😂", "hi"): "क्या पागलपन है हाहाहा 😂",
    ("yaar kya scene hai", "en"): "dude, what's the plan?",
}


class FakeProvider:
    """Scriptable provider. Records jobs; can be told to fail N times or return given outputs."""

    def __init__(self, name: str, *, supported: set[str] | None = None, latency: float = 0.0) -> None:
        self.name = name
        self.jobs: list[tuple[TranslationJob, bool]] = []
        self.fail: list[ErrorKind] = []
        self.overrides: dict[str, str] = {}
        self.supported = supported
        self.latency = latency
        self.confidence = 0.9

    def supports(self, source: str | None, target: str) -> bool:
        return self.supported is None or (target in self.supported and (source is None or source in self.supported))

    async def translate(self, job: TranslationJob, *, quality: bool, timeout: float) -> TranslationResult:
        self.jobs.append((job, quality))
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.fail:
            raise ProviderError(self.fail.pop(0), self.name, "scripted failure")
        plain = html_to_plain(job.text) if job.html else job.text
        out = self.overrides.get(plain) or DICTIONARY.get((plain, job.target)) or f"[{job.target}] {plain}"
        return TranslationResult(True, out, job.source_hint or "es", self.confidence, self.name,
                                 "quality" if quality else "fast")

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None
