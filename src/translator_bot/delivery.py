"""Private delivery of translations: Telegram ephemeral messages (Bot API 10.2+), never public.

Each recipient is delivered independently (separate task, separate error handling). If ephemeral delivery
fails, the only fallback is a private chat message to a recipient who explicitly consented to it.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import EphemeralMessageParameters, LinkPreviewOptions, ReplyParameters

from translator_bot.languages import lang_name
from translator_bot.metrics import Metrics
from translator_bot.recipients import Recipient
from translator_bot.resilience import KeyedLimiter, SlidingWindowLimiter
from translator_bot.storage import Storage
from translator_bot.textutil import html_to_plain, sanitize_html, truncate

log = logging.getLogger(__name__)

MAX_TEXT = 4096


class DeliveryError(Exception):
    def __init__(self, reason: str, *, permanent: bool = False, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason, self.permanent, self.retry_after = reason, permanent, retry_after


class Transport(Protocol):
    async def send_ephemeral(self, chat_id: int, user_id: int, text_html: str, *, reply_to: int | None,
                             thread_id: int | None) -> int: ...

    async def edit_ephemeral(self, chat_id: int, user_id: int, ephemeral_id: int, text_html: str) -> None: ...

    async def send_private(self, user_id: int, text_html: str) -> int: ...


class TelegramTransport:
    """aiogram-backed transport with global + per-chat send limiters and Telegram error mapping."""

    def __init__(self, bot: Bot, *, global_per_s: int = 25, chat_per_min: int = 60) -> None:
        self.bot = bot
        self._global = SlidingWindowLimiter(global_per_s, 1.0)
        self._per_chat = KeyedLimiter(chat_per_min, 60.0)

    async def _limit(self, chat_id: int) -> None:
        if not await self._global.acquire(max_wait=10.0):
            raise DeliveryError("global send limit")
        if not await self._per_chat.get(chat_id).acquire(max_wait=20.0):
            raise DeliveryError("chat send limit")

    @staticmethod
    def _map(exc: TelegramAPIError) -> DeliveryError:
        msg = str(exc).lower()
        if isinstance(exc, TelegramRetryAfter):
            return DeliveryError("retry_after", retry_after=float(exc.retry_after))
        if isinstance(exc, TelegramForbiddenError):
            return DeliveryError("forbidden", permanent=True)
        if isinstance(exc, TelegramBadRequest):
            if "can't parse entities" in msg or "unsupported start tag" in msg:
                return DeliveryError("parse_error")
            # wording varies by API version: live 2026-11-22 Telegram returned "REPLY_MESSAGE_ID_INVALID"
            if ("message to be replied not found" in msg or "replied message not found" in msg
                    or "reply_message_id_invalid" in msg or "message to reply not found" in msg):
                return DeliveryError("reply_not_found")
            # specific outcomes first: their texts can also mention "ephemeral"
            if "message is not modified" in msg:
                return DeliveryError("not_modified", permanent=True)
            if "user not found" in msg or "participant" in msg or "not a member" in msg:
                return DeliveryError("not_member", permanent=True)
            if "not enough rights" in msg or "administrator" in msg or "ephemeral" in msg:
                return DeliveryError("ephemeral_not_allowed", permanent=True)
            return DeliveryError(f"bad_request:{msg[:60]}", permanent=True)
        return DeliveryError(f"api:{type(exc).__name__}")

    async def send_ephemeral(self, chat_id: int, user_id: int, text_html: str, *, reply_to: int | None,
                             thread_id: int | None) -> int:
        await self._limit(chat_id)
        try:
            msg = await self.bot.send_message(
                chat_id=chat_id, text=text_html, parse_mode="HTML",
                message_thread_id=thread_id,
                ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=user_id),
                reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                disable_notification=True,
            )
        except TelegramAPIError as exc:
            raise self._map(exc) from None
        if not msg.ephemeral_message_id:
            log.warning("ephemeral send returned no ephemeral_message_id")
            return 0  # recorded as "no id": later edits send a fresh message instead of editing
        return int(msg.ephemeral_message_id)

    async def edit_ephemeral(self, chat_id: int, user_id: int, ephemeral_id: int, text_html: str) -> None:
        await self._limit(chat_id)
        try:
            await self.bot.edit_ephemeral_message_text(
                chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=ephemeral_id, text=text_html,
                parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramAPIError as exc:
            raise self._map(exc) from None

    async def send_private(self, user_id: int, text_html: str) -> int:
        if user_id <= 0:
            raise DeliveryError("not a private chat", permanent=True)
        if not await self._global.acquire(max_wait=10.0):
            raise DeliveryError("global send limit")
        try:
            msg = await self.bot.send_message(chat_id=user_id, text=text_html, parse_mode="HTML",
                                              link_preview_options=LinkPreviewOptions(is_disabled=True),
                                              disable_notification=True)
        except TelegramAPIError as exc:
            raise self._map(exc) from None
        return msg.message_id


# --- formatting ----------------------------------------------------------------------------------


@dataclass(slots=True)
class Delivery:
    """One translated item to deliver to one or more recipients sharing a target language."""

    chat_id: int
    chat_title: str | None
    thread_id: int | None
    message_ids: list[int]  # originals (a burst may cover several); reply targets the last one
    sender_name: str
    source_lang: str | None
    target_lang: str
    translation_html: str
    kind: str = "text"  # text | caption | sticker | gif | image | album
    original_plain: str | None = None  # only used when the chat enables show_original
    edited: bool = False
    show_sender: bool = False


def format_delivery(d: Delivery, *, notice: str | None = None) -> str:
    src = lang_name(d.source_lang) if d.source_lang else None
    meta = " · ".join(x for x in (
        {"text": None, "caption": "caption", "sticker": "sticker", "gif": "GIF", "image": "image",
         "album": "album", "video": "video", "voice": "voice"}.get(d.kind),
        f"{src} → {lang_name(d.target_lang)}" if src else lang_name(d.target_lang),
        "edited" if d.edited else None) if x)
    # The translation is delivered as a reply to the original, so the sender is visible from the quote;
    # the name is shown only if the chat enables show_sender.
    who = f"<b>{html.escape(d.sender_name or 'Someone')}</b> " if d.show_sender else ""
    head = f"🌐 {who}<i>{html.escape(meta)}</i>"
    parts = [head]
    if d.original_plain:
        parts.append(f"<blockquote>{html.escape(truncate(d.original_plain, 300))}</blockquote>")
    parts.append(sanitize_html(d.translation_html))
    if notice:
        parts.append(f"<i>{html.escape(notice)}</i>")
    text = "\n".join(parts)
    if len(text) > MAX_TEXT:  # keep valid HTML: truncate the plain body instead of slicing tags
        body = truncate(html_to_plain(d.translation_html), MAX_TEXT - len(head) - 200)
        text = "\n".join([head, html.escape(body)] + ([f"<i>{html.escape(notice)}</i>"] if notice else []))
    return text


def format_private_fallback(d: Delivery, link: str | None) -> str:
    title = html.escape(d.chat_title or "a group")
    body = format_delivery(d)
    tail = f'\n<a href="{html.escape(link, quote=True)}">Open message</a>' if link else ""
    return f"<i>From {title}</i>\n{body}{tail}"


def message_link(chat_id: int, message_id: int, thread_id: int | None) -> str | None:
    s = str(chat_id)
    if not s.startswith("-100"):
        return None  # basic groups have no message links
    internal = s[4:]
    return f"https://t.me/c/{internal}/{thread_id}/{message_id}" if thread_id else f"https://t.me/c/{internal}/{message_id}"


DEFAULT_NOTICE = "English is selected by default. Open a private chat with me and send /language to change it."


# --- deliverer -----------------------------------------------------------------------------------


@dataclass
class FailureDigest:
    """Aggregates delivery failures for the owner; flushed periodically by DM (never to the group)."""

    counts: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))
    titles: dict[int, str] = field(default_factory=dict)

    def add(self, chat_id: int, title: str | None, reason: str) -> None:
        self.counts[(chat_id, reason)] += 1
        if title:
            self.titles[chat_id] = title

    def drain(self) -> str | None:
        if not self.counts:
            return None
        lines = ["⚠️ <b>Private delivery failures</b> (last period)"]
        for (chat_id, reason), n in sorted(self.counts.items()):
            lines.append(f"• {html.escape(self.titles.get(chat_id, str(chat_id)))}: {html.escape(reason)} × {n}")
        self.counts.clear()
        return "\n".join(lines)


_inflight: set[asyncio.Task[None]] = set()


async def _finish(coros: Iterable[Coroutine[Any, Any, None]]) -> None:
    """Run per-recipient sends concurrently and shield them from the job's timeout/cancellation: a send that
    reached Telegram is always recorded (an unrecorded ephemeral would be duplicated by a later edit)."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    for t in tasks:
        _inflight.add(t)
        t.add_done_callback(_inflight.discard)
    await asyncio.gather(*(asyncio.shield(t) for t in tasks), return_exceptions=True)


async def drain_deliveries(timeout: float) -> None:
    """Shutdown: wait for shielded per-recipient sends that outlived their (cancelled) jobs."""
    if _inflight:
        await asyncio.wait(set(_inflight), timeout=timeout)


class Deliverer:
    def __init__(self, transport: Transport, storage: Storage, metrics: Metrics,
                 member_check: Callable[[int, int], Awaitable[bool]] | None = None) -> None:
        self.transport = transport
        self.storage = storage
        self.metrics = metrics
        self.member_check = member_check
        self.digest = FailureDigest()

    async def deliver(self, d: Delivery, recipients: list[Recipient], *, show_original: bool = False) -> None:
        """Deliver to every recipient concurrently; one recipient's failure never affects another."""
        await _finish(self._deliver_one(d, r, show_original) for r in recipients)

    async def _deliver_one(self, d: Delivery, r: Recipient, show_original: bool) -> None:
        anchor = d.message_ids[-1]
        notice = DEFAULT_NOTICE if r.notice_pending else None
        view = d if show_original else replace(d, original_plain=None)
        text = format_delivery(view, notice=notice)
        t0 = time.perf_counter()
        try:
            eph_id = await self._send_with_retries(d.chat_id, r.user_id, text, anchor, d.thread_id, view, notice)
        except DeliveryError as err:
            self.metrics.inc(f"delivery_fail.ephemeral.{err.reason.split(':')[0]}")
            self.digest.add(d.chat_id, d.chat_title, f"ephemeral {err.reason.split(':')[0]}")
            for mid in d.message_ids:
                await self.storage.record_delivery(d.chat_id, mid, r.user_id, channel="ephemeral", status="failed",
                                                   lang=d.target_lang, error=err.reason[:80])
            await self._fallback(d, r, err)
            return
        self.metrics.inc("delivered.ephemeral")
        self.metrics.observe("delivery_ms", (time.perf_counter() - t0) * 1000)
        for mid in d.message_ids:
            await self.storage.record_delivery(d.chat_id, mid, r.user_id, channel="ephemeral", status="sent",
                                               lang=d.target_lang, ephemeral_id=eph_id)
        if notice:
            await self.storage.upsert_user(r.user_id, default_notice_sent=True)

    async def _send_with_retries(self, chat_id: int, user_id: int, text: str, anchor: int | None,
                                 thread_id: int | None, d: Delivery, notice: str | None) -> int:
        reply_to = anchor
        for attempt in range(3):
            try:
                return await self.transport.send_ephemeral(chat_id, user_id, text, reply_to=reply_to,
                                                           thread_id=thread_id)
            except DeliveryError as err:
                if err.reason == "reply_not_found" and reply_to is not None:
                    reply_to = None  # original deleted meanwhile; deliver without the reply link
                    continue
                if err.reason == "parse_error":
                    plain = replace(d, translation_html=html.escape(html_to_plain(d.translation_html)))
                    text = format_delivery(plain, notice=notice)
                    continue
                if err.retry_after is not None and err.retry_after <= 5 and attempt < 2:
                    self.metrics.inc("delivery_retry_after")
                    await asyncio.sleep(err.retry_after)
                    continue
                if err.reason.startswith("api:") and attempt == 0:
                    # network blip / Telegram 5xx: one jittered retry. Rarely the first request did arrive and
                    # the reader sees the translation twice - better than losing it (observed: laptop outages). Jitter only.
                    self.metrics.inc("delivery_retry_transient")
                    await asyncio.sleep(1.0 + random.random())  # nosec B311
                    continue
                raise
        raise DeliveryError("retries exhausted")

    async def _fallback(self, d: Delivery, r: Recipient, err: DeliveryError) -> None:
        if not r.dm_fallback:
            return  # no consent -> record only. NEVER downgrade to public delivery.
        if err.reason in ("not_member", "forbidden"):
            return  # removed from the group (or blocked us): their access ends here
        if self.member_check is not None:
            try:
                still_member = await self.member_check(d.chat_id, r.user_id)  # uncached, right before DM
            except Exception:
                still_member = False
            if not still_member:
                self.metrics.inc("delivery_fail.dm.not_member")
                return
        try:
            mid = await self.transport.send_private(r.user_id, format_private_fallback(
                d, message_link(d.chat_id, d.message_ids[-1], d.thread_id)))
        except DeliveryError as err2:
            self.metrics.inc(f"delivery_fail.dm.{err2.reason.split(':')[0]}")
            if err2.reason == "forbidden":  # user blocked the bot: stop trying DM until they /start again
                await self.storage.upsert_user(r.user_id, started=False)
            return
        self.metrics.inc("delivered.dm_fallback")
        for m in d.message_ids:
            await self.storage.record_delivery(d.chat_id, m, r.user_id, channel="dm", status="sent",
                                               lang=d.target_lang, dm_message_id=mid, error=err.reason[:80])

    async def update(self, d: Delivery, recipients: list[Recipient], *, show_original: bool = False) -> None:
        """Message edited: edit existing ephemeral translations in place; send new ones where none exist."""
        existing = {row["recipient_id"]: row for mid in d.message_ids
                    for row in await self.storage.deliveries_for(d.chat_id, mid)}
        tasks = []
        for r in recipients:
            row = existing.get(r.user_id)
            if row and row["channel"] == "ephemeral" and row["status"] == "sent" and row["ephemeral_id"]:
                tasks.append(self._edit_one(d, r, int(row["ephemeral_id"]), show_original))
            else:
                tasks.append(self._deliver_one(d, r, show_original))
        await _finish(tasks)

    async def _edit_one(self, d: Delivery, r: Recipient, eph_id: int, show_original: bool) -> None:
        view = d if show_original else replace(d, original_plain=None)
        text = format_delivery(view)
        for attempt in range(3):
            try:
                await self.transport.edit_ephemeral(d.chat_id, r.user_id, eph_id, text)
                self.metrics.inc("delivery_edit.ok")
                return
            except DeliveryError as err:
                if err.reason == "not_modified":
                    return
                if err.reason == "parse_error" and attempt == 0:  # same plain-text fallback as sends
                    text = format_delivery(replace(view, translation_html=html.escape(html_to_plain(
                        view.translation_html))))
                    continue
                if err.retry_after is not None and err.retry_after <= 5 and attempt < 2:
                    await asyncio.sleep(err.retry_after)
                    continue
                self.metrics.inc(f"delivery_edit.fail.{err.reason.split(':')[0]}")
                if err.permanent and err.reason not in ("not_member", "forbidden", "parse_error"):
                    # the ephemeral is gone (expired / not found): deliver a fresh one marked edited
                    await self._deliver_one(d, r, show_original)
                # transient failures (rate limits, network) keep the old translation rather than duplicating it
                return

