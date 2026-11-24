"""Group message pipeline: eligibility -> burst coalescing -> per-language detection & translation ->
per-recipient private (ephemeral) delivery. Handlers call `on_message` / `on_edit` and return immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html as _html
import logging
import time
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from aiogram.types import Message

from translator_bot.config import Settings
from translator_bot.context import ContextStore
from translator_bot.delivery import BUBBLE_WINDOW_S, Deliverer, Delivery
from translator_bot.detect import LanguageDetector, Verdict
from translator_bot.eligibility import Eligible, Kind, Skip, classify, sender_id, sender_name
from translator_bot.languages import LANGUAGES, same_language
from translator_bot.media.processor import AUDIO_KINDS, MediaProcessor, MediaText
from translator_bot.media.processor import PERSISTED as PERSISTED_KINDS
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import ContextLine, TranslationJob
from translator_bot.recipients import Recipient, RecipientResolver
from translator_bot.storage import Chat, Storage
from translator_bot.textutil import restrict_links, truncate
from translator_bot.workqueue import BurstCoalescer, BurstItem, Lane

log = logging.getLogger(__name__)

MEDIA_KIND = {Kind.IMAGE: "image", Kind.CAPTION: "image", Kind.STICKER: "sticker",
              Kind.ANIMATED_STICKER: "animated_sticker", Kind.VIDEO_STICKER: "video_sticker", Kind.GIF: "gif",
              Kind.VIDEO: "video", Kind.VOICE: "voice"}


@dataclass(slots=True)
class PrivateResult:
    text: str | None  # Telegram HTML, or None when there is nothing to send
    src: str | None = None
    reason: str = ""  # when text is None: "same" | "no_text" | "too_long" | "slow" | "failed"


@dataclass(slots=True)
class UnitMeta:
    chat_id: int
    chat_title: str | None
    thread_id: int | None
    sender_id: int | None
    sender_name: str
    reply_ctx: ContextLine | None
    settings: dict[str, Any]
    ctx_before: int | None = None  # ContextStore.mark when the (first) message arrived
    append_after: int | None = None  # exact preceding original; never merge out-of-order translations
    arrival_context: tuple[ContextLine, ...] | None = None
    parent_id: int | None = None


@dataclass(slots=True)
class UnitRecord:
    """Kept in memory only (bounded TTL) so edits can re-render a burst. Never persisted."""

    meta: UnitMeta
    items: list[BurstItem]
    created: float = field(default_factory=time.monotonic)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class Pipeline:
    def __init__(self, settings: Settings, storage: Storage, detector: LanguageDetector, orchestrator: Orchestrator,
                 resolver: RecipientResolver, deliverer: Deliverer, context: ContextStore, metrics: Metrics,
                 media: MediaProcessor | None, *, bot_id: int, burst_window: float = 0.7,
                 edit_memory_s: float = 1800) -> None:
        self.s, self.storage, self.detector, self.orch = settings, storage, detector, orchestrator
        self.resolver, self.deliverer, self.context, self.metrics, self.media = resolver, deliverer, context, metrics, media
        self.bot_id = bot_id
        self.text_lane = Lane("text", workers=settings.text_workers, max_total=settings.queue_max,
                              per_chat_max=settings.per_chat_queue_max, job_timeout=45, metrics=metrics)
        self.media_lane = Lane("media", workers=settings.media_workers, max_total=max(20, settings.queue_max // 5),
                               per_chat_max=max(3, settings.per_chat_queue_max // 4), job_timeout=90, metrics=metrics)
        self.bursts = BurstCoalescer(self._on_burst, window=burst_window)
        self.edit_memory_s = edit_memory_s
        self._units: OrderedDict[tuple[int, int], UnitRecord] = OrderedDict()  # (chat, msg) -> unit
        self._locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        # last media reading per message (memory only, edit_memory_s): a caption edit reuses it instead of
        # re-downloading and re-running vision/audio for photos, videos and voice (which aren't cached on disk)
        self._media_memo: OrderedDict[tuple[int, int], tuple[float, str, MediaText | None]] = OrderedDict()
        self._bg: set[asyncio.Task[None]] = set()
        self._chat_cache: dict[int, tuple[float, Chat]] = {}
        self._lang_votes: OrderedDict[int, deque[str]] = OrderedDict()  # approved sender -> recent languages
        self._participants: set[tuple[int, int]] = set()  # (chat, user) already stored as participant
        self._group_daily: dict[int, tuple[str, int]] = {}  # chat -> (UTC day, translated messages)
        self._last_speaker: dict[tuple[int, int | None], tuple[int | None, float, int]] = {}

    def start(self) -> None:
        self.text_lane.start()
        self.media_lane.start()

    async def stop(self) -> None:
        for t in list(self._bg):
            t.cancel()
        await self.bursts.flush_all()
        await self.text_lane.stop()
        await self.media_lane.stop()

    # -- chat config (cached 30s) ----------------------------------------------------------------
    async def chat(self, msg: Message) -> Chat:
        now = time.monotonic()
        hit = self._chat_cache.get(msg.chat.id)
        if hit and now - hit[0] < 30:
            return hit[1]
        chat = await self.storage.get_chat(msg.chat.id)
        if chat is None:
            # First sight without a my_chat_member event (e.g. fresh database): respect ENABLE_NEW_GROUPS.
            # "owner_added" can't be verified here, so the chat stays disabled until an owner enables it.
            chat = await self.storage.upsert_chat(msg.chat.id, title=msg.chat.title, type=msg.chat.type, active=True,
                                                  enabled=self.s.enable_new_groups == "all")
        elif chat.title != msg.chat.title:
            chat = await self.storage.upsert_chat(msg.chat.id, title=msg.chat.title)
        self._chat_cache[msg.chat.id] = (now, chat)
        return chat

    def invalidate_chat(self, chat_id: int) -> None:
        self._chat_cache.pop(chat_id, None)

    # -- entry points (never block: enqueue and return) ------------------------------------------
    async def on_message(self, msg: Message) -> None:
        self.metrics.inc("updates.message")
        chat = await self.chat(msg)
        sid = sender_id(msg)
        thread = topic_of(msg)  # one key for context, bursts, claims and delivery
        ctx_mark = self.context.mark  # context = what was said *before* this message
        arrival_context = (self.context.get(msg.chat.id, thread, sid or 0, limit=self.s.context_messages)
                           if chat.active and chat.enabled and chat.settings.get("context", True)
                           and self.s.context_messages else ())
        reply_ctx = self._reply_ctx(msg, sid)
        prev = self._last_speaker.get((msg.chat.id, thread))
        now = time.monotonic()
        append_after = (prev[2] if prev is not None and sid is not None and prev[0] == sid
                        and now - prev[1] <= BUBBLE_WINDOW_S and not msg.reply_to_message else None)
        self._last_speaker[(msg.chat.id, thread)] = (sid, now, msg.message_id)
        if prev is not None and (prev[0] != sid or not msg.text or msg.reply_to_message):
            await self.bursts.flush((msg.chat.id, thread, prev[0] or 0))
        if len(self._last_speaker) > 20_000:
            self._last_speaker.clear()
        if msg.text and sid is not None and not msg.text.startswith("/"):
            self.context.add(msg.chat.id, thread, sid, msg.text)
        elif msg.caption and sid is not None:
            self.context.add(msg.chat.id, thread, sid, msg.caption)
        if not chat.enabled or not chat.active:
            self.metrics.inc("skip.chat_disabled")
            return
        if not chat.bot_is_admin:
            # Telegram only lets admin bots send ephemeral messages at any time: translating would burn
            # provider quota on undeliverable output. Owners are told by the setup report.
            self.metrics.inc("skip.not_admin")
            return
        if not await self.storage.topic_enabled(msg.chat.id, thread):
            self.metrics.inc("skip.topic_disabled")
            return
        if sid is not None and sid > 0 and msg.from_user is not None and not msg.from_user.is_bot:
            raw = msg.text or msg.caption or ""
            if (msg.chat.id, sid) not in self._participants:
                self._participants.add((msg.chat.id, sid))
                if len(self._participants) > 100_000:
                    self._participants.clear()
                self._spawn(self.storage.mark_participant(sid, msg.chat.id))
            self._spawn(self._learn_language(sid, msg.from_user.language_code,
                                             "" if raw.startswith("/") else raw[:500], msg.chat.id))
        res = classify(msg, bot_id=self.bot_id, settings=chat.settings, media_enabled=self.s.media_enabled)
        if isinstance(res, Skip):
            self.metrics.inc(f"skip.{res.value}")
            return
        if len(res.plain) > self.s.max_text_chars:
            res = Eligible(res.kind, html_escape_truncate(res.plain, self.s.max_text_chars), truncate(res.plain, self.s.max_text_chars), res.has_media)
        if not self._group_quota_ok(msg.chat.id):
            self.metrics.inc("skip.group_daily_limit")
            return
        if not await self.storage.claim_message(msg.chat.id, msg.message_id, thread, sid,
                                                content_hash(res.plain + str(res.kind.value) + (_media_file(msg)[1] or ""))):
            self.metrics.inc("skip.duplicate")
            return
        self.metrics.inc(f"eligible.{res.kind.value}")
        meta = UnitMeta(msg.chat.id, msg.chat.title, thread, sid, sender_name(msg), reply_ctx,
                        chat.settings, ctx_mark, append_after, arrival_context,
                        msg.reply_to_message.message_id if reply_ctx and msg.reply_to_message else None)
        if res.kind is Kind.TEXT:
            await self.bursts.add((msg.chat.id, thread, sid or 0),
                                  BurstItem(msg.message_id, res.text_html, res.plain), meta)
            if reply_ctx:
                await self.bursts.flush((msg.chat.id, thread, sid or 0))
        else:
            key = (msg.chat.id, thread, sid or 0)
            await self.bursts.flush(key)  # keep ordering: pending text from this sender goes first
            if not await self.media_lane.submit(msg.chat.id, lambda: self._process_media(msg, res, meta),
                                                key=f"m:{msg.chat.id}:{msg.message_id}"):
                await self.storage.update_message(msg.chat.id, msg.message_id, status="queue_rejected")

    async def on_edit(self, msg: Message) -> None:
        self.metrics.inc("updates.edit")
        chat = await self.chat(msg)
        if not chat.enabled or not chat.active or not chat.bot_is_admin:
            return
        thread = topic_of(msg)
        # same gates as new messages: disabled topics stay silent
        if not await self.storage.topic_enabled(msg.chat.id, thread):
            self.metrics.inc("skip.topic_disabled")
            return
        # only messages *sent* recently are retranslated (edit_date is always ~now, so compare the send date)
        sent = msg.date.timestamp() if msg.date else 0.0
        if time.time() - sent > self.s.edit_window_s:
            self.metrics.inc("edit.too_old")
            return
        res = classify(msg, bot_id=self.bot_id, settings=chat.settings, media_enabled=self.s.media_enabled)
        if isinstance(res, Skip) or res.kind not in (Kind.TEXT, Kind.CAPTION, Kind.GIF, Kind.IMAGE, Kind.VIDEO,
                                                     Kind.VOICE):
            return
        if len(res.plain) > self.s.max_text_chars:
            res = Eligible(res.kind, html_escape_truncate(res.plain, self.s.max_text_chars),
                           truncate(res.plain, self.s.max_text_chars), res.has_media)
        sid = sender_id(msg)
        new_hash = content_hash(res.plain + str(res.kind.value) + (_media_file(msg)[1] or ""))
        row = await self.storage.get_message(msg.chat.id, msg.message_id)
        if row is not None and row["content_hash"] == new_hash:
            self.metrics.inc("edit.unchanged")  # e.g. link preview loaded / reactions: nothing to redo
            return
        if self.bursts.update_pending((msg.chat.id, thread, sid or 0), msg.message_id, res.text_html, res.plain):
            self.metrics.inc("edit.in_pending_burst")
            return
        if row is None:
            if not await self.storage.claim_message(msg.chat.id, msg.message_id, thread, sid, new_hash):
                return
        else:
            await self.storage.update_message(msg.chat.id, msg.message_id, content_hash=new_hash)
        if res.has_media:
            # caption edit (or replaced file) on media: re-render caption *and* media text together - a caption-only
            # re-translation used to overwrite the combined ephemeral and drop the image/GIF text
            meta_m = UnitMeta(msg.chat.id, msg.chat.title, thread, sid, sender_name(msg), self._reply_ctx(msg, sid),
                              chat.settings)
            self.metrics.inc("edit.retranslate_media")
            await self._submit_when_unlocked(self.media_lane, msg.chat.id, [msg.message_id],
                                             lambda: self._process_media(msg, res, meta_m, edited=True),
                                             key=f"em:{msg.chat.id}:{msg.message_id}:{new_hash}")
            return
        unit = self._units.get((msg.chat.id, msg.message_id))
        if unit is not None and time.monotonic() - unit.created <= self.edit_memory_s:
            for it in unit.items:
                if it.message_id == msg.message_id:
                    it.text_html, it.plain = res.text_html, res.plain
            items, meta = unit.items, unit.meta
        else:
            items = [BurstItem(msg.message_id, res.text_html, res.plain)]
            meta = UnitMeta(msg.chat.id, msg.chat.title, thread, sid, sender_name(msg), self._reply_ctx(msg, sid),
                            chat.settings)
        self.metrics.inc("edit.retranslate")
        await self._submit_when_unlocked(self.text_lane, msg.chat.id, [i.message_id for i in items],
                                         lambda: self._process_text(meta, items, edited=True),
                                         key=f"e:{msg.chat.id}:{msg.message_id}:{new_hash}")

    # -- internals -------------------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def _message_locks(self, chat_id: int, message_ids: list[int]) -> AsyncIterator[None]:
        """Serialize the original translation and edits of the same message(s): an edit racing the original
        job sent a duplicate (and possibly stale) ephemeral. Sorted order avoids lock-order deadlocks."""
        keys = sorted({(chat_id, m) for m in message_ids})
        held: list[tuple[tuple[int, int], asyncio.Lock]] = []
        try:
            for k in keys:
                lk = self._locks[k]
                await lk.acquire()
                held.append((k, lk))
            yield
        finally:  # also runs when cancelled half-way through acquiring: never leak a held lock
            for k, lk in held:
                lk.release()
                if not lk.locked() and not getattr(lk, "_waiters", None):
                    self._locks.pop(k, None)

    async def _submit_when_unlocked(self, lane: Lane, chat_id: int, message_ids: list[int],
                                    fn: Callable[[], Awaitable[None]], *, key: str) -> None:
        """Queue an edit job; if the original job for these messages still holds their lock, wait for it in the
        background first, so the wait doesn't eat the edit job's own lane timeout."""
        busy = [self._locks[k] for k in ((chat_id, m) for m in message_ids)
                if k in self._locks and self._locks[k].locked()]
        if not busy:
            await lane.submit(chat_id, fn, key=key)
            return

        async def later() -> None:
            for lk in busy:
                async with lk:
                    pass
            await lane.submit(chat_id, fn, key=key)

        task = asyncio.create_task(later())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _group_quota_ok(self, chat_id: int) -> bool:
        """GROUP_DAILY_LIMIT translated messages per group per UTC day (in memory; a restart resets it)."""
        limit = self.s.group_daily_limit
        if limit <= 0:
            return True
        today = time.strftime("%Y-%m-%d", time.gmtime())
        day, n = self._group_daily.get(chat_id, (today, 0))
        if day != today:
            n = 0
        if n >= limit:
            return False
        self._group_daily[chat_id] = (today, n + 1)
        if len(self._group_daily) > 50_000:
            self._group_daily = {k: v for k, v in self._group_daily.items() if v[0] == today}
        return True

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _learn_language(self, uid: int, telegram_lang: str | None, plain: str,
                              chat_id: int | None = None) -> None:
        """Automatic reader language for approved members who never chose one: the language they write in the
        group (majority of their last 5 messages), else their Telegram app language. Codes only, never text."""
        try:
            approved = (await self.resolver.is_approved_in(chat_id, uid) if chat_id is not None
                        else await self.resolver.is_approved(uid))
            if not approved:
                return
            user = await self.storage.get_user(uid)
            if user is not None and user.lang:
                return  # an explicit choice always wins
            fields: dict[str, str] = {}
            tg = _supported_lang(telegram_lang)
            if tg and (user is None or user.tg_lang != tg):
                fields["tg_lang"] = tg
            if len(plain.split()) >= 2 or (plain and not plain.isascii()):
                det = await asyncio.to_thread(self.detector.detect, plain, "en")
                code = _supported_lang(det.lang) if not det.neutral and det.confidence >= 0.6 else None
                if code:
                    votes = self._lang_votes.setdefault(uid, deque(maxlen=5))
                    self._lang_votes.move_to_end(uid)
                    while len(self._lang_votes) > 10_000:
                        self._lang_votes.popitem(last=False)
                    votes.append(code)
                    top, n = Counter(votes).most_common(1)[0]
                    if n >= 2 and (user is None or user.lang_guess != top):
                        fields["lang_guess"] = top
            if fields:
                await self.storage.upsert_user(uid, **fields)
        except Exception:
            log.debug("language learning failed", exc_info=True)

    async def _already_delivered(self, chat_id: int, message_ids: list[int]) -> bool:
        for m in message_ids:
            if await self.storage.deliveries_for(chat_id, m):
                return True
        return False

    async def _edit_would_split_burst(self, chat_id: int, items: list[BurstItem]) -> bool:
        """True when the edited message shares its ephemeral with other messages this edit doesn't cover
        (burst memory expired / restart): editing in place would erase their translations."""
        ids = {i.message_id for i in items}
        for m in ids:
            for row in await self.storage.deliveries_for(chat_id, m):
                eph = row.get("ephemeral_id")
                if eph and set(await self.storage.messages_for_ephemeral(chat_id, int(eph),
                                                                         int(row["recipient_id"]))) - ids:
                    return True
        return False

    def _reply_ctx(self, msg: Message, sid: int | None) -> ContextLine | None:
        r = msg.reply_to_message
        if r is None or r.chat.id != msg.chat.id or r.ephemeral_message_id is not None or r.forum_topic_created:
            return None
        quote = msg.quote.text if msg.quote is not None else None
        text = quote or (r.text or r.caption if r is not None else None)
        if not text:
            return None
        other = sender_id(r) if r is not None else None
        label = self.context.speaker_label(msg.chat.id, topic_of(msg), sid or 0, other or -1)
        return ContextLine(label, truncate(text, 300))

    async def _translated_parent(self, meta: UnitMeta, lang: str,
                                 glossary: tuple[tuple[str, str], ...]) -> str | None:
        if not meta.reply_ctx or not meta.settings.get("context", True):
            return None
        source = meta.reply_ctx.text
        # Translate the quoted speaker independently. Later conversation belongs to the reply.
        parent = replace(meta, reply_ctx=None, settings={"context": False}, arrival_context=())
        record = self._units.get((meta.chat_id, meta.parent_id)) if meta.parent_id is not None else None
        if (record and record.meta.thread_id == meta.thread_id and record.meta.arrival_context is not None
                and time.monotonic() - record.created <= min(self.edit_memory_s, self.context.ttl_s)
                and len(record.items) == 1 and source in record.items[0].plain):
            parent = record.meta
        out, _, why = await self._translate_outcome(parent, source, source, lang, glossary=glossary)
        if why == "same":
            return source
        return _html.unescape(out) if out else None

    async def _on_burst(self, key: tuple[int, int | None, int], items: list[BurstItem], meta: object) -> None:
        assert isinstance(meta, UnitMeta)
        if len(items) > 1:
            self.metrics.inc("burst.coalesced", len(items))
        if not await self.text_lane.submit(meta.chat_id, lambda: self._process_text(meta, items),
                                           key=f"u:{meta.chat_id}:{items[0].message_id}"):
            for item in items:
                await self.storage.update_message(meta.chat_id, item.message_id, status="queue_rejected")

    def _remember(self, meta: UnitMeta, items: list[BurstItem]) -> None:
        rec = UnitRecord(meta, items)
        for it in items:
            self._units[(meta.chat_id, it.message_id)] = rec
        while self._units:
            _k, oldest = next(iter(self._units.items()))
            if time.monotonic() - oldest.created > self.edit_memory_s or len(self._units) > 20_000:
                self._units.popitem(last=False)
            else:
                break

    def _job(self, meta: UnitMeta, text_html: str, plain: str, target: str, verdict: Verdict, det_lang: str | None,
             romanized: bool, mixed: bool, style: str, glossary: tuple[tuple[str, str], ...]) -> TranslationJob:
        words = len(plain.split())
        use_ctx = meta.settings.get("context", True) and (words <= 8 or meta.reply_ctx is not None
                                                          or verdict is Verdict.UNSURE)
        ctx: tuple[ContextLine, ...] = ()
        if use_ctx and self.s.context_messages:
            ctx = meta.arrival_context if meta.arrival_context is not None else self.context.get(
                meta.chat_id, meta.thread_id, meta.sender_id or 0, limit=self.s.context_messages, before=meta.ctx_before)
            # no arrival mark (edit after restart): the current message itself is the last entry - drop it
            if meta.ctx_before is None and ctx and ctx[-1].text.strip() == truncate(plain, 280).strip():
                ctx = ctx[:-1]
        is_html = text_html != plain and "<" in text_html
        return TranslationJob(
            text=text_html if is_html else plain, target=target, source_hint=det_lang if verdict is Verdict.OTHER else None,
            context=ctx, reply_to=meta.reply_ctx if use_ctx else None, glossary=glossary, style=style,  # type: ignore[arg-type]
            html=is_html, romanized=romanized, mixed=mixed,
            unsure=verdict is Verdict.UNSURE,
        )

    async def _translate_for(self, meta: UnitMeta, text_html: str, plain: str, target: str, *, style: str = "chat",
                             glossary: tuple[tuple[str, str], ...] = ()) -> tuple[str, str | None] | None:
        out, src, _why = await self._translate_outcome(meta, text_html, plain, target, style=style, glossary=glossary)
        return (out, src) if out is not None else None

    async def _translate_outcome(self, meta: UnitMeta, text_html: str, plain: str, target: str, *,
                                 style: str = "chat", glossary: tuple[tuple[str, str], ...] = ()
                                 ) -> tuple[str | None, str | None, str]:
        """(translation HTML or None, source language, reason): reason is "" | "same" | "failed"."""
        det = await asyncio.to_thread(self.detector.detect, plain, target)
        if det.verdict is Verdict.TARGET:
            self.metrics.inc("skip.already_target")
            return None, det.lang, "same"
        job = self._job(meta, text_html, plain, target, det.verdict, det.lang, det.romanized, det.mixed, style,
                        glossary)
        res = await self.orch.translate(job, det, purpose="ocr" if style == "ocr" else "group",
                                        scope=str(meta.chat_id))
        if res is None:
            return None, det.lang, "failed"
        src = res.source_lang or det.lang
        # Providers report Chinese as plain "zh" for both scripts; the detector's Han-variant verdict is the
        # authority there (a Traditional message for a Simplified reader is a real conversion, not a no-op).
        if det.lang and det.lang.lower().startswith("zh") and src and src.lower().startswith("zh"):
            src = det.lang
        if src and same_language(src, target):
            return None, src, "same"
        # Output is Telegram HTML. Plain sources: escape, so the model can't add markup (link spoofing).
        # HTML sources: keep formatting, but only links that were already in the message.
        out = restrict_links(res.translation, job.text) if job.html else _html.escape(res.translation, quote=False)
        return out, src, ""

    # -- private chat (1:1 translator) -------------------------------------------------------------
    async def recover_group(self, msg: Message, el: Eligible, lang: str, settings: dict[str, Any]) -> PrivateResult:
        if el.has_media or not el.plain:
            return await self.translate_private(msg, el, lang)
        rec = self._units.get((msg.chat.id, msg.message_id))
        meta = UnitMeta(msg.chat.id, msg.chat.title, topic_of(msg), sender_id(msg), sender_name(msg),
                        self._reply_ctx(msg, sender_id(msg)), settings, arrival_context=(),
                        parent_id=msg.reply_to_message.message_id if msg.reply_to_message else None)
        if (rec and rec.meta.thread_id == meta.thread_id and settings.get("context", True)
                and time.monotonic() - rec.created <= min(self.edit_memory_s, self.context.ttl_s)):
            meta = replace(meta, arrival_context=rec.meta.arrival_context or (),
                           reply_ctx=meta.reply_ctx or rec.meta.reply_ctx,
                           parent_id=meta.parent_id or rec.meta.parent_id)
        glossary = tuple(await self.storage.glossary(msg.chat.id))
        (out, src, why), parent = await asyncio.gather(
            self._translate_outcome(meta, el.text_html, el.plain, lang, glossary=glossary),
            self._translated_parent(meta, lang, glossary))
        if parent and (out or why == "same"):
            out = f"<blockquote>{_html.escape(truncate(parent, 240))}</blockquote>\n" + (out or _html.escape(el.plain))
        return PrivateResult(out, src, why)

    async def translate_private(self, msg: Message, el: Eligible, target: str) -> PrivateResult:
        """Translate one message sent to the bot in a private chat into `target`. Nothing is stored; the
        media cache (by Telegram file id) is shared with groups."""
        uid = msg.chat.id
        meta = UnitMeta(uid, None, None, uid, "", None, {"context": False})
        parts: list[str] = []
        src: str | None = None
        reasons: list[str] = []
        if el.plain:
            out, s, why = await self._translate_outcome(meta, el.text_html, el.plain, target)
            src = s
            if out:
                parts.append(out)
            else:
                reasons.append(why)
        file_id, unique_id, size, duration = _media_file(msg)
        mkind = MEDIA_KIND.get(el.kind)
        if el.has_media and self.media is not None and file_id and unique_id and mkind:
            if mkind in AUDIO_KINDS and duration is not None and duration > self.s.max_media_seconds:
                reasons.append("too_long")
            else:
                mt = await self.media.extract(file_id=file_id, file_unique_id=unique_id, size=size,
                                              kind=mkind, targets=[target],  # type: ignore[arg-type]
                                              caption_hint=el.plain or None, duration=duration)
                if mt is None and self.media.failure(unique_id) == "timeout":
                    reasons.append("slow")  # never claim "no text" for a file we couldn't finish reading
                elif mt is None or not mt.text or mt.text.strip() == el.plain.strip():
                    reasons.append("no_text")
                else:
                    cached = mt.translations.get(target)
                    tr: str | None
                    if cached is not None:
                        tr = _html.escape(cached, quote=False) if cached else None
                        why = "" if cached else "same"
                    else:
                        tr, s, why = await self._translate_outcome(meta, mt.text, mt.text, target, style="ocr")
                        src = src or s
                    src = src or mt.source_lang
                    if tr:
                        icon = {Kind.VIDEO: "🎬 ", Kind.VOICE: "🎙 "}.get(el.kind, "🖼 ")
                        parts.append((icon if el.plain else "") + tr)
                    else:
                        reasons.append(why)
        self.metrics.inc("private.translated" if parts else "private.nothing")
        if parts:
            return PrivateResult("\n".join(parts), src)
        for why in ("failed", "slow", "too_long", "same", "no_text"):
            if why in reasons:
                return PrivateResult(None, src, why)
        return PrivateResult(None, src, "no_text")

    async def _recipients_by_lang(self, meta: UnitMeta) -> dict[str, list[Recipient]]:
        recipients = await self.resolver.resolve(meta.chat_id, meta.sender_id)
        by_lang: dict[str, list[Recipient]] = defaultdict(list)
        for r in recipients:
            by_lang[r.lang].append(r)
        return by_lang

    async def _filter_readers(self, plain: str, target: str, rs: list[Recipient]) -> list[Recipient]:
        # Only skip confidently single-language content. A provider's single source label cannot
        # establish that every part of a code-switched message is understood by this reader.
        if not plain or not any(r.also_reads for r in rs):
            return rs
        det = await asyncio.to_thread(self.detector.detect, plain, target)
        if det.mixed or det.verdict is not Verdict.OTHER or not det.lang:
            return rs
        kept = [r for r in rs if not r.reads(det.lang)]
        self.metrics.inc("skip.reader_reads_source", len(rs) - len(kept))
        return kept

    async def _current_readers(self, meta: UnitMeta, lang: str, rs: list[Recipient]) -> list[Recipient]:
        # Provider calls may take seconds: revocation, pause, or a changed language during that wait
        # must take effect before delivery. Never add readers who weren't in the original request.
        ids = {r.user_id for r in rs}
        return [r for r in await self.resolver.resolve(meta.chat_id, meta.sender_id)
                if r.user_id in ids and r.lang == lang]

    async def _process_text(self, meta: UnitMeta, items: list[BurstItem], *, edited: bool = False) -> None:
        ids = [i.message_id for i in items]
        async with self._message_locks(meta.chat_id, ids):
            if not edited and await self._already_delivered(meta.chat_id, ids):
                self.metrics.inc("skip.superseded_by_edit")  # an edit already delivered the newer text
                return
            fresh = edited and await self._edit_would_split_burst(meta.chat_id, items)
            await self._process_text_locked(meta, items, edited=edited, fresh=fresh)

    async def _process_text_locked(self, meta: UnitMeta, items: list[BurstItem], *, edited: bool,
                                   fresh: bool) -> None:
        t0 = time.perf_counter()
        by_lang = await self._recipients_by_lang(meta)
        self._remember(meta, items)
        if not by_lang:
            self.metrics.inc("skip.no_recipients")
            return
        text_html = "\n".join(i.text_html for i in items)
        plain = "\n".join(i.plain for i in items)
        glossary = tuple(await self.storage.glossary(meta.chat_id))

        async def one(lang: str, rs: list[Recipient]) -> None:
            rs = await self._filter_readers(plain, lang, rs)
            if not rs:
                return
            parent = None
            if meta.reply_ctx and meta.settings.get("context", True):
                (translation, src, reason), parent = await asyncio.gather(
                    self._translate_outcome(meta, text_html, plain, lang, glossary=glossary),
                    self._translated_parent(meta, lang, glossary),
                )
                if translation is None:
                    if reason != "same" or not parent or parent == meta.reply_ctx.text:
                        return
                    # The reply is readable, but its parent still needs translating.
                    translation = _html.escape(plain)
            else:
                out = await self._translate_for(meta, text_html, plain, lang, glossary=glossary)
                if out is None:
                    return
                translation, src = out
            rs = await self._filter_readers(plain, lang, await self._current_readers(meta, lang, rs))
            if not rs:
                return
            d = Delivery(meta.chat_id, meta.chat_title, meta.thread_id, [i.message_id for i in items],
                         meta.sender_name, src, lang, translation, "text", plain, edited,
                         show_sender=bool(meta.settings.get("show_sender")), sender_id=meta.sender_id,
                         append_after=meta.append_after if not edited else None, parent_excerpt=parent)
            if edited and not fresh:
                await self.deliverer.update(d, rs, show_original=bool(meta.settings.get("show_original")))
            else:  # new message, or an edit that must not overwrite a burst's shared ephemeral
                await self.deliverer.deliver(d, rs, show_original=bool(meta.settings.get("show_original")))

        await asyncio.gather(*(one(lang, rs) for lang, rs in by_lang.items()), return_exceptions=False)
        for it in items:
            await self.storage.update_message(meta.chat_id, it.message_id, status="done")
        self.metrics.observe("pipeline_text_ms", (time.perf_counter() - t0) * 1000)

    async def _process_media(self, msg: Message, el: Eligible, meta: UnitMeta, *, edited: bool = False) -> None:
        async with self._message_locks(meta.chat_id, [msg.message_id]):
            if not edited and await self._already_delivered(meta.chat_id, [msg.message_id]):
                self.metrics.inc("skip.superseded_by_edit")
                return
            await self._process_media_locked(msg, el, meta, edited=edited)

    async def _process_media_locked(self, msg: Message, el: Eligible, meta: UnitMeta, *, edited: bool) -> None:
        t0 = time.perf_counter()
        by_lang = await self._recipients_by_lang(meta)
        if not by_lang:
            self.metrics.inc("skip.no_recipients")
            return
        file_id, unique_id, size, duration = _media_file(msg)
        mkind = MEDIA_KIND.get(el.kind)
        langs = list(by_lang)
        glossary = tuple(await self.storage.glossary(meta.chat_id))
        # media extraction (one provider call for all languages) runs concurrently with caption translation
        extract_task: asyncio.Task[MediaText | None] | None = None
        memo_key = (meta.chat_id, msg.message_id)
        memo = self._media_memo.get(memo_key)
        reuse = (edited and memo is not None and memo[1] == unique_id
                 and time.monotonic() - memo[0] <= self.edit_memory_s)
        if reuse:
            self.metrics.inc("edit.media_reused")
        elif el.has_media and self.media is not None and file_id and unique_id and mkind:
            extract_task = asyncio.create_task(self.media.extract(
                file_id=file_id, file_unique_id=unique_id, size=size, kind=mkind,  # type: ignore[arg-type]
                targets=langs, caption_hint=el.plain or None, duration=duration))

        async def caption_for(lang: str) -> tuple[str, str | None] | None:
            if not el.plain:
                return None
            return await self._translate_for(meta, el.text_html, el.plain, lang, glossary=glossary)

        try:
            captions = dict(zip(langs, await asyncio.gather(*(caption_for(lg) for lg in langs)), strict=True))
            if reuse:
                assert memo is not None
                media_text = memo[2]
            else:
                media_text = await extract_task if extract_task is not None else None
        finally:
            if extract_task is not None and not extract_task.done():
                extract_task.cancel()  # caption failed / job cancelled: stop the download/ffmpeg/vision work
        if unique_id:
            self._media_memo[memo_key] = (time.monotonic(), unique_id, media_text)
            self._media_memo.move_to_end(memo_key)
            while self._media_memo and (len(self._media_memo) > 2000 or time.monotonic()
                                        - next(iter(self._media_memo.values()))[0] > self.edit_memory_s):
                self._media_memo.popitem(last=False)
        icon = {Kind.VIDEO: "🎬 ", Kind.VOICE: "🎙 "}.get(el.kind, "🖼 ")

        async def one(lang: str, rs: list[Recipient]) -> None:
            parts: list[str] = []
            src: str | None = None
            cap = captions.get(lang)
            if cap:
                parts.append(cap[0])
                src = cap[1]
            if media_text is not None and media_text.text and media_text.text.strip() != el.plain.strip():
                # media_text.translations (and the media cache) hold plain text; `tr` is escaped HTML
                cached_tr = media_text.translations.get(lang)
                if cached_tr is not None:
                    tr = _html.escape(cached_tr, quote=False)  # vision output is plain text, never markup
                else:  # not produced by the media call (cache from other languages / local OCR)
                    out, s, why = await self._translate_outcome(meta, media_text.text, media_text.text, lang,
                                                                style="ocr")
                    tr = out or ""  # plain OCR job -> already escaped
                    src = src or s
                    # remember only real outcomes: a failure (quota, outage) must not become "nothing to translate"
                    if why != "failed":
                        media_text.translations[lang] = _html.unescape(tr)
                    # caption-steered readings stay out of the shared (file id) cache, like in the processor
                    if why != "failed" and unique_id and mkind in PERSISTED_KINDS and not el.plain:
                        await self.storage.put_media(unique_id, has_text=True, text=media_text.text,
                                                     source_lang=media_text.source_lang or src,
                                                     confidence=media_text.confidence,
                                                     translations=media_text.translations)
                if tr:
                    src = src or media_text.source_lang
                    parts.append((icon if el.plain else "") + tr)
            if not parts:
                return
            parent = await self._translated_parent(meta, lang, glossary)
            rs = await self._current_readers(meta, lang, rs)
            caption_readers = await self._filter_readers(el.plain, lang, rs)
            media_readers = await self._filter_readers(media_text.text if media_text else "", lang, rs)
            kind = {Kind.STICKER: "sticker", Kind.ANIMATED_STICKER: "sticker", Kind.VIDEO_STICKER: "sticker",
                    Kind.GIF: "gif", Kind.IMAGE: "image", Kind.VIDEO: "video", Kind.VOICE: "voice"}.get(el.kind,
                                                                                                       "caption")
            async def deliver_reader(r: Recipient) -> None:
                selected = list(parts)
                originals: list[str] = []
                if cap and r in caption_readers:
                    originals.append(el.plain)
                if len(parts) > int(bool(cap)) and r in media_readers and media_text:
                    originals.append(media_text.text)
                if cap and r not in caption_readers:
                    selected = selected[1:]
                if len(parts) > int(bool(cap)) and r not in media_readers:
                    selected = selected[:-1]
                if not selected:
                    return
                d = Delivery(meta.chat_id, meta.chat_title, meta.thread_id, [msg.message_id], meta.sender_name,
                             src, lang, "\n".join(selected), kind,
                             "\n".join(originals) or None, edited,
                             show_sender=bool(meta.settings.get("show_sender")), parent_excerpt=parent)
                if edited:
                    await self.deliverer.update(d, [r], show_original=bool(meta.settings.get("show_original")))
                else:
                    await self.deliverer.deliver(d, [r], show_original=bool(meta.settings.get("show_original")))

            await asyncio.gather(*(deliver_reader(r) for r in rs))

        await asyncio.gather(*(one(lang, rs) for lang, rs in by_lang.items()))
        await self.storage.update_message(meta.chat_id, msg.message_id, status="done")
        self.metrics.observe(f"pipeline_media_ms.{el.kind.value}", (time.perf_counter() - t0) * 1000)



def _supported_lang(code: str | None) -> str | None:
    """A language code the bot supports (Telegram sends e.g. "en", "pt-br", "zh-hans"), or None."""
    if not code:
        return None
    c = code.strip()
    if c in LANGUAGES:
        return c
    low = c.lower()
    if low in ("zh-hant", "zh-tw", "zh-hk", "zh-mo"):
        return "zh-Hant" if "zh-Hant" in LANGUAGES else None
    base = low.split("-")[0]
    return base if base in LANGUAGES else None

def topic_of(msg: Message) -> int | None:
    """Forum topic id, or None. Non-forum replies may carry message_thread_id; it is not a topic."""
    return msg.message_thread_id if msg.is_topic_message else None


def html_escape_truncate(plain: str, limit: int) -> str:
    import html as _h

    return _h.escape(truncate(plain, limit), quote=False)


def _media_file(msg: Message) -> tuple[str | None, str | None, int | None, float | None]:
    """(file_id, file_unique_id, size, duration_seconds) of the media worth processing."""
    if msg.sticker is not None:
        st = msg.sticker
        return st.file_id, st.file_unique_id, st.file_size, None
    if msg.animation is not None:
        a = msg.animation
        return a.file_id, a.file_unique_id, a.file_size, float(a.duration)
    if msg.video is not None:
        v = msg.video
        return v.file_id, v.file_unique_id, v.file_size, float(v.duration)
    if msg.video_note is not None:
        n = msg.video_note
        return n.file_id, n.file_unique_id, n.file_size, float(n.duration)
    if msg.voice is not None:
        vo = msg.voice
        return vo.file_id, vo.file_unique_id, vo.file_size, float(vo.duration)
    if msg.audio is not None:
        au = msg.audio
        return au.file_id, au.file_unique_id, au.file_size, float(au.duration)
    if msg.photo:
        # largest size not exceeding ~1280px on the long side (enough for OCR, smaller download)
        sizes = sorted(msg.photo, key=lambda p: p.width * p.height)
        pick = sizes[0]
        for p in sizes:
            if max(p.width, p.height) <= 1280:
                pick = p
        return pick.file_id, pick.file_unique_id, pick.file_size, None
    return None, None, None, None
