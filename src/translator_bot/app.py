"""Application container: builds every component, verifies group setup, runs maintenance tasks."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from translator_bot.config import Settings
from translator_bot.context import ContextStore
from translator_bot.delivery import Deliverer, TelegramTransport, drain_deliveries
from translator_bot.detect import LanguageDetector
from translator_bot.guard import PublicOutputGuard
from translator_bot.media.processor import MediaProcessor
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.pipeline import Pipeline
from translator_bot.providers.base import TranslationProvider, VisionProvider
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.recipients import MembershipCache, RecipientResolver
from translator_bot.resilience import KeyedLimiter
from translator_bot.storage import Storage

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


@dataclass(slots=True)
class SetupReport:
    chat_id: int
    title: str | None
    is_admin: bool
    chat_type: str
    can_read_all: bool
    problems: list[str]

    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [f"<b>Setup check — {html.escape(self.title or str(self.chat_id))}</b>"]
        lines.append(("✅" if self.is_admin else "❌") + " Bot is an administrator (required for private in-group "
                     "translations; no admin rights need to be enabled)")
        lines.append(("✅" if self.chat_type == "supergroup" else "ℹ️") + f" Chat type: {self.chat_type}")
        lines.append("✅ Receives all messages (admin bots always do)" if self.is_admin else
                     ("✅" if self.can_read_all else "❌") + " Privacy mode off / can read all messages")
        for p in self.problems:
            lines.append(f"⚠️ {html.escape(p)}")
        if self.ok():
            lines.append("\nEverything looks good. The bot will stay completely silent in the group.")
        return "\n".join(lines)


class App:
    def __init__(self, settings: Settings, *, session: BaseSession | None = None,
                 providers: dict[str, TranslationProvider] | None = None,
                 detector: LanguageDetector | None = None, vision: VisionProvider | None = None) -> None:
        """`session` / `providers` are injection points for tests; production uses the defaults."""
        self.s = settings
        self.metrics = Metrics()
        self.guard = PublicOutputGuard()
        self.bot = Bot(settings.bot_token.get_secret_value(), session=session,
                       default=DefaultBotProperties(parse_mode="HTML"))
        self._injected_providers = providers
        self._injected_vision = vision
        self.bot.session.middleware(self.guard)
        self.storage = Storage(settings.database_path)
        self.detector: LanguageDetector | None = detector
        self.providers: dict[str, TranslationProvider] = {}
        self.gemini: GeminiProvider | None = None
        self.deepl: DeepLProvider | None = None
        self.context = ContextStore(settings.context_messages, settings.context_ttl_s)
        self.membership = MembershipCache(self._is_member)
        self.resolver = RecipientResolver(self.storage, self.membership, settings.owner_ids,
                                          default_lang=settings.default_target_lang,
                                          onboarding_required=settings.onboarding_required)
        self.transport = TelegramTransport(self.bot, global_per_s=settings.global_sends_per_s,
                                           chat_per_min=settings.ephemeral_per_chat_per_min)
        self.deliverer = Deliverer(self.transport, self.storage, self.metrics, member_check=self._is_member)
        self.orchestrator: Orchestrator | None = None
        self.fatal = asyncio.Event()  # set by the watchdog: __main__ stops and exits non-zero
        self.fatal_reason: str | None = None
        self.media: MediaProcessor | None = None
        self.pipeline: Pipeline | None = None
        self.inline_limiter = KeyedLimiter(settings.inline_per_min, 60.0)
        self.private_limiter = KeyedLimiter(settings.private_per_min, 60.0)
        self.private_daily: dict[int, tuple[str, int]] = {}  # user id -> (UTC day, count); unapproved users only
        self.bot_username = ""
        self.bot_id = settings.bot_id
        self._tasks: list[asyncio.Task[None]] = []

    # -- lifecycle -------------------------------------------------------------------------------
    async def start(self) -> None:
        await self.storage.open()
        if self._injected_providers is not None:
            self.providers = dict(self._injected_providers)
        elif self.s.gemini_api_key:
            self.gemini = GeminiProvider(
                self.s.gemini_api_key.get_secret_value(), fast_model=self.s.gemini_fast_model,
                quality_model=self.s.gemini_quality_model, vision_model=self.s.gemini_vision_model,
                fallback_model=self.s.gemini_fallback_model, fast_thinking=self.s.gemini_fast_thinking,
                quality_thinking=self.s.gemini_quality_thinking, rpm=self.s.gemini_rpm, rpd=self.s.gemini_rpd, store=self.s.gemini_store,
                hedge_s=self.s.gemini_hedge_s, base_url=self.s.gemini_base_url)
            self.providers["gemini"] = self.gemini
        if self.s.deepl_api_key and self._injected_providers is None:
            self.deepl = DeepLProvider(self.s.deepl_api_key.get_secret_value(), rpm=self.s.deepl_rpm,
                                       monthly_char_budget=self.s.deepl_monthly_char_budget)
            try:
                await self.deepl.load_languages()
                await self.deepl.refresh_usage()
                self.providers["deepl"] = self.deepl
            except Exception as exc:  # keep running on Gemini alone
                log.error("DeepL unavailable at startup", extra={"error": str(exc)[:120]})
        if not self.providers:
            raise RuntimeError("no translation provider is usable (check API keys)")
        if self.gemini is not None:
            await self._check_gemini_models()
        if self.detector is None:
            self.detector = await asyncio.to_thread(LanguageDetector, wordlist=self.s.detector_wordlist)
        self.orchestrator = Orchestrator(self.providers, self.metrics, provider_timeout=self.s.provider_timeout_s)
        vision: VisionProvider | None = self._injected_vision or (
            self.gemini if (self.gemini and self.s.vision_ocr) else None)
        if self.s.media_enabled:
            self.media = MediaProcessor(
                self.bot, self.storage, self.metrics, vision=vision, max_download_bytes=self.s.max_download_bytes,
                max_image_pixels=self.s.max_image_pixels, max_tgs_json_bytes=self.s.max_tgs_json_bytes,
                max_video_seconds=self.s.max_video_seconds, max_frames=self.s.max_frames,
                ffmpeg_path=self.s.ffmpeg_path, timeout_s=self.s.media_timeout_s, workers=self.s.media_workers,
                ocr_min_confidence=self.s.ocr_min_confidence, cache_ttl_s=self.s.cache_ttl_days * 86400,
                max_media_seconds=self.s.max_media_seconds, max_video_bytes=self.s.max_video_bytes,
                ocr_gate_frames=self.s.ocr_gate_frames)
            await self.media.warmup()
        self.pipeline = Pipeline(self.s, self.storage, self.detector, self.orchestrator, self.resolver,
                                 self.deliverer, self.context, self.metrics, self.media, bot_id=self.bot_id,
                                 burst_window=self.s.burst_window_ms / 1000)
        self.pipeline.start()
        me = await self.bot.get_me()
        self.bot_username = me.username or ""
        if not me.supports_inline_queries:
            log.warning("inline mode is disabled in BotFather (/setinline)")
        self._tasks.append(asyncio.create_task(self._maintenance(), name="maintenance"))
        self._tasks.append(asyncio.create_task(self._watchdog(), name="watchdog"))

    async def _check_gemini_models(self) -> None:
        assert self.gemini is not None
        try:
            available = set(await self.gemini.list_models())
        except Exception as exc:
            log.error("Gemini model listing failed", extra={"error": str(exc)[:160]})
            return
        for m in {self.s.gemini_fast_model, self.s.gemini_quality_model, self.s.gemini_vision_model,
                  self.s.gemini_fallback_model}:
            if not m:
                continue
            if m not in available:
                log.error("configured Gemini model not available for this key", extra={"model": m})

    async def health(self) -> dict[str, object]:
        """Liveness for /healthz (and Docker's HEALTHCHECK). Unhealthy = a restart would help: pipeline not
        running, a queue worker died, or the database doesn't answer. Provider outages are reported, not fatal
        (a restart can't fix Gemini being down)."""
        problems: list[str] = []
        lanes: dict[str, object] = {}
        if self.pipeline is None:
            problems.append("pipeline not started")
        else:
            for lane in (self.pipeline.text_lane, self.pipeline.media_lane):
                alive = sum(1 for t in lane._tasks if not t.done())
                lanes[lane.name] = {"queued": lane.size, "inflight": lane.inflight, "workers_alive": alive,
                                    "workers": lane.workers}
                if alive < lane.workers:
                    problems.append(f"{lane.name} lane: {lane.workers - alive} worker(s) dead")
        try:
            await self.storage.ping()
        except Exception as exc:
            problems.append(f"database: {type(exc).__name__}")
        breakers = {n: b.state for n, b in self.orchestrator.breakers.items()} if self.orchestrator else {}
        return {"ok": not problems, "problems": problems, "lanes": lanes, "providers": breakers,
                "guard_blocked": self.guard.blocked}

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.pipeline:
            await self.pipeline.stop()
        await drain_deliveries(10.0)  # shielded sends still in flight must record before the DB closes
        await self._flush_digest()
        if self.media:
            await self.media.close()
        for p in self.providers.values():
            with contextlib.suppress(Exception):
                await p.aclose()
        await self.storage.close()
        await self.bot.session.close()

    async def _watchdog(self, interval: float = 60.0) -> None:
        """Self-healing: when health problems persist across two checks (dead queue worker, database not
        answering), request a clean shutdown with a non-zero exit so Docker/systemd restart the process.
        (A Docker "unhealthy" status alone never triggers a restart.)"""
        strikes = 0
        while True:
            await asyncio.sleep(interval)
            try:
                h = await self.health()
            except Exception as exc:  # health itself failing counts as a strike
                h = {"ok": False, "problems": [f"health check failed: {type(exc).__name__}"]}
            strikes = 0 if h["ok"] else strikes + 1
            if strikes >= 2:
                problems = h["problems"]
                self.fatal_reason = "; ".join(str(x) for x in problems) if isinstance(problems, list) else str(problems)
                log.critical("unhealthy, exiting for restart", extra={"problems": h["problems"]})
                self.fatal.set()
                return

    async def _maintenance(self) -> None:
        while True:
            await asyncio.sleep(self.s.failure_digest_minutes * 60)
            try:
                await self.storage.cleanup(self.s.message_map_retention_h * 3600, self.s.cache_ttl_days * 86400)
                await self._flush_digest()
                if self.deepl:
                    await self.deepl.refresh_usage()
            except Exception:
                log.exception("maintenance failed")

    async def _flush_digest(self) -> None:
        text = self.deliverer.digest.drain()
        if not text:
            return
        for oid in self.s.owner_ids:
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(oid, text)  # private chat only (positive id)

    # -- membership & setup ----------------------------------------------------------------------
    async def _is_member(self, chat_id: int, user_id: int) -> bool:
        m = await self.bot.get_chat_member(chat_id, user_id)
        if m.status in ("left", "kicked"):
            return False
        if m.status == "restricted":
            return bool(getattr(m, "is_member", False))
        return m.status in MEMBER_STATUSES

    async def verify_setup(self, chat_id: int) -> SetupReport:
        problems: list[str] = []
        chat = await self.bot.get_chat(chat_id)
        me_member = await self.bot.get_chat_member(chat_id, self.bot_id)
        is_admin = isinstance(me_member, (ChatMemberAdministrator, ChatMemberOwner))
        me = await self.bot.get_me()
        can_read = bool(me.can_read_all_group_messages)
        if not is_admin:
            problems.append("Promote the bot to administrator (you can switch every admin right OFF). Without "
                            "admin status Telegram does not let bots send private in-group messages, so the bot "
                            "stays silent here.")
        if chat.type == "group":
            problems.append("Basic group: works, but message links for the private-chat backup are unavailable. "
                            "Converting to a supergroup is recommended.")
        await self.storage.upsert_chat(chat_id, title=chat.title, type=chat.type, bot_is_admin=is_admin, active=True)
        if self.pipeline:
            self.pipeline.invalidate_chat(chat_id)
        recips = await self.resolver.candidates()
        eligible = 0
        for u in recips:
            self.membership.invalidate(chat_id, u.user_id)
            with contextlib.suppress(Exception):
                if await self.membership.is_member(chat_id, u.user_id):
                    eligible += 1
        if eligible == 0:
            problems.append("No approved recipient is a member of this group yet.")
        return SetupReport(chat_id, chat.title, is_admin, chat.type, can_read, problems)

    async def notify_owners(self, text: str) -> None:
        for oid in self.s.owner_ids:
            with contextlib.suppress(TelegramAPIError):
                await self.bot.send_message(oid, text)
