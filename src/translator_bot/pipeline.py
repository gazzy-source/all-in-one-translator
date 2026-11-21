"""Group message pipeline: eligibility -> burst coalescing -> per-language detection & translation ->
per-recipient private (ephemeral) delivery. Handlers call `on_message` / `on_edit` and return immediately.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as _html
import logging
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any

from aiogram.types import Message

from translator_bot.config import Settings
from translator_bot.context import ContextStore
from translator_bot.delivery import Deliverer, Delivery
from translator_bot.detect import LanguageDetector, Verdict
from translator_bot.eligibility import Eligible, Kind, Skip, classify, sender_id, sender_name
from translator_bot.media.processor import PERSISTED as PERSISTED_KINDS
from translator_bot.media.processor import MediaProcessor, MediaText
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
class UnitMeta:
    chat_id: int
    chat_title: str | None
    thread_id: int | None
    sender_id: int | None
    sender_name: str
    reply_ctx: ContextLine | None
    settings: dict[str, Any]


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
        self._chat_cache: dict[int, tuple[float, Chat]] = {}

    def start(self) -> None:
        self.text_lane.start()
        self.media_lane.start()

    async def stop(self) -> None:
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
        res = classify(msg, bot_id=self.bot_id, settings=chat.settings, media_enabled=self.s.media_enabled)
        if isinstance(res, Skip):
            self.metrics.inc(f"skip.{res.value}")
            return
        if len(res.plain) > self.s.max_text_chars:
            res = Eligible(res.kind, html_escape_truncate(res.plain, self.s.max_text_chars), truncate(res.plain, self.s.max_text_chars), res.has_media)
        if not await self.storage.claim_message(msg.chat.id, msg.message_id, thread, sid,
                                                content_hash(res.plain + str(res.kind.value))):
            self.metrics.inc("skip.duplicate")
            return
        self.metrics.inc(f"eligible.{res.kind.value}")
        meta = UnitMeta(msg.chat.id, msg.chat.title, thread, sid, sender_name(msg), self._reply_ctx(msg, sid),
                        chat.settings)
        if res.kind is Kind.TEXT:
            await self.bursts.add((msg.chat.id, thread, sid or 0),
                                  BurstItem(msg.message_id, res.text_html, res.plain), meta)
        else:
            key = (msg.chat.id, thread, sid or 0)
            await self.bursts.flush(key)  # keep ordering: pending text from this sender goes first
            await self.media_lane.submit(msg.chat.id, lambda: self._process_media(msg, res, meta),
                                         key=f"m:{msg.chat.id}:{msg.message_id}")

    async def on_edit(self, msg: Message) -> None:
        self.metrics.inc("updates.edit")
        chat = await self.chat(msg)
        if not chat.enabled or not chat.active or not chat.bot_is_admin:
            return
        res = classify(msg, bot_id=self.bot_id, settings=chat.settings, media_enabled=self.s.media_enabled)
        if isinstance(res, Skip) or res.kind not in (Kind.TEXT, Kind.CAPTION, Kind.GIF, Kind.IMAGE):
            return
        if msg.edit_date and time.time() - msg.edit_date > self.s.edit_window_s:
            return
        thread = topic_of(msg)
        sid = sender_id(msg)
        new_hash = content_hash(res.plain + str(res.kind.value))
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
        if res.kind is not Kind.TEXT and not res.plain:
            return  # media edits (replaced file) are not re-OCR'd; caption edits are handled below
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
        await self.text_lane.submit(msg.chat.id, lambda: self._process_text(meta, items, edited=True),
                                    key=f"e:{msg.chat.id}:{msg.message_id}:{new_hash}")

    # -- internals -------------------------------------------------------------------------------
    def _reply_ctx(self, msg: Message, sid: int | None) -> ContextLine | None:
        r = msg.reply_to_message
        quote = msg.quote.text if msg.quote is not None else None
        text = quote or (r.text or r.caption if r is not None else None)
        if not text:
            return None
        other = sender_id(r) if r is not None else None
        label = self.context.speaker_label(msg.chat.id, topic_of(msg), sid or 0, other or -1)
        return ContextLine(label, truncate(text, 300))

    async def _on_burst(self, key: tuple[int, int | None, int], items: list[BurstItem], meta: object) -> None:
        assert isinstance(meta, UnitMeta)
        if len(items) > 1:
            self.metrics.inc("burst.coalesced", len(items))
        await self.text_lane.submit(meta.chat_id, lambda: self._process_text(meta, items),
                                    key=f"u:{meta.chat_id}:{items[0].message_id}")

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
            ctx = self.context.get(meta.chat_id, meta.thread_id, meta.sender_id or 0, limit=self.s.context_messages)
            # the current message itself is the last context entry: drop it
            if ctx and ctx[-1].text.strip() == truncate(plain, 280).strip():
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
        det = await asyncio.to_thread(self.detector.detect, plain, target)
        if det.verdict is Verdict.TARGET:
            self.metrics.inc("skip.already_target")
            return None
        job = self._job(meta, text_html, plain, target, det.verdict, det.lang, det.romanized, det.mixed, style,
                        glossary)
        res = await self.orch.translate(job, det, purpose="ocr" if style == "ocr" else "group",
                                        scope=str(meta.chat_id))
        if res is None:
            return None
        src = res.source_lang or det.lang
        if src and src.split("-")[0] == target.split("-")[0]:
            return None
        # Output is Telegram HTML. Plain sources: escape, so the model can't add markup (link spoofing).
        # HTML sources: keep formatting, but only links that were already in the message.
        out = restrict_links(res.translation, job.text) if job.html else _html.escape(res.translation, quote=False)
        return out, src

    async def _recipients_by_lang(self, meta: UnitMeta) -> dict[str, list[Recipient]]:
        recipients = await self.resolver.resolve(meta.chat_id, meta.sender_id)
        by_lang: dict[str, list[Recipient]] = defaultdict(list)
        for r in recipients:
            by_lang[r.lang].append(r)
        return by_lang

    async def _process_text(self, meta: UnitMeta, items: list[BurstItem], *, edited: bool = False) -> None:
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
            out = await self._translate_for(meta, text_html, plain, lang, glossary=glossary)
            if out is None:
                return
            translation, src = out
            d = Delivery(meta.chat_id, meta.chat_title, meta.thread_id, [i.message_id for i in items],
                         meta.sender_name, src, lang, translation, "text", plain, edited,
                         show_sender=bool(meta.settings.get("show_sender")))
            if edited:
                await self.deliverer.update(d, rs, show_original=bool(meta.settings.get("show_original")))
            else:
                await self.deliverer.deliver(d, rs, show_original=bool(meta.settings.get("show_original")))

        await asyncio.gather(*(one(lang, rs) for lang, rs in by_lang.items()), return_exceptions=False)
        for it in items:
            await self.storage.update_message(meta.chat_id, it.message_id, status="done")
        self.metrics.observe("pipeline_text_ms", (time.perf_counter() - t0) * 1000)

    async def _process_media(self, msg: Message, el: Eligible, meta: UnitMeta) -> None:
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
        if el.has_media and self.media is not None and file_id and unique_id and mkind:
            extract_task = asyncio.create_task(self.media.extract(
                file_id=file_id, file_unique_id=unique_id, size=size, kind=mkind,  # type: ignore[arg-type]
                targets=langs, caption_hint=el.plain or None, duration=duration))

        async def caption_for(lang: str) -> tuple[str, str | None] | None:
            if not el.plain:
                return None
            return await self._translate_for(meta, el.text_html, el.plain, lang, glossary=glossary)

        captions = dict(zip(langs, await asyncio.gather(*(caption_for(lg) for lg in langs)), strict=True))
        media_text = await extract_task if extract_task is not None else None
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
                    out = await self._translate_for(meta, media_text.text, media_text.text, lang, style="ocr")
                    tr = out[0] if out else ""  # plain OCR job -> already escaped by _translate_for
                    src = src or (out[1] if out else None)
                    media_text.translations[lang] = _html.unescape(tr)
                    if unique_id and mkind in PERSISTED_KINDS:
                        await self.storage.put_media(unique_id, has_text=True, text=media_text.text,
                                                     source_lang=media_text.source_lang or src,
                                                     confidence=media_text.confidence,
                                                     translations=media_text.translations)
                if tr:
                    src = src or media_text.source_lang
                    parts.append((icon if el.plain else "") + tr)
            if not parts:
                return
            kind = {Kind.STICKER: "sticker", Kind.ANIMATED_STICKER: "sticker", Kind.VIDEO_STICKER: "sticker",
                    Kind.GIF: "gif", Kind.IMAGE: "image", Kind.VIDEO: "video", Kind.VOICE: "voice"}.get(el.kind,
                                                                                                       "caption")
            d = Delivery(meta.chat_id, meta.chat_title, meta.thread_id, [msg.message_id], meta.sender_name, src, lang,
                         "\n".join(parts), kind, el.plain or (media_text.text if media_text else None),
                         show_sender=bool(meta.settings.get("show_sender")))
            await self.deliverer.deliver(d, rs, show_original=bool(meta.settings.get("show_original")))

        await asyncio.gather(*(one(lang, rs) for lang, rs in by_lang.items()))
        await self.storage.update_message(meta.chat_id, msg.message_id, status="done")
        self.metrics.observe(f"pipeline_media_ms.{el.kind.value}", (time.perf_counter() - t0) * 1000)


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
