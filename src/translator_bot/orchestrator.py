"""Translation orchestrator: routing between providers, validation, caching, retries and fallback.

Routing (free-tier aware):
  * clean, standard text DeepL supports        -> DeepL (generous free quota, excellent for prose)
  * informal / slang / romanized / mixed / short / uncertain -> Gemini fast (understands chat)
  * ambiguous-in-context, mixed+romanized, OCR text, or a failed validation -> Gemini quality
  * provider failure -> exactly one hop to the other provider family (no cascades)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Literal

from translator_bot.detect import Detection, Verdict
from translator_bot.languages import LANGUAGES
from translator_bot.metrics import Metrics
from translator_bot.providers.base import (
    FALLBACK_OK,
    RETRYABLE,
    ErrorKind,
    ProviderError,
    TranslationJob,
    TranslationProvider,
    TranslationResult,
)
from translator_bot.resilience import CircuitBreaker, backoff_delay
from translator_bot.textutil import (
    html_to_plain,
    looks_garbled,
    missing_entities,
    normalize_for_key,
    romanized_output,
    wrong_script,
)

log = logging.getLogger(__name__)

Purpose = Literal["group", "inline", "ocr"]


@dataclass(frozen=True, slots=True)
class Route:
    provider: str  # "gemini" | "deepl"
    quality: bool
    reason: str


class TTLCache:
    """In-memory LRU with TTL. Message translations are never persisted to disk (privacy)."""

    def __init__(self, max_items: int = 5000, ttl: float = 6 * 3600) -> None:
        self.max_items, self.ttl = max_items, ttl
        self._d: OrderedDict[str, tuple[float, TranslationResult]] = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key: str) -> TranslationResult | None:
        item = self._d.get(key)
        if item is None or time.monotonic() - item[0] > self.ttl:
            if item is not None:
                del self._d[key]
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return replace(item[1], cached=True, latency_ms=0.0)

    def put(self, key: str, value: TranslationResult) -> None:
        self._d[key] = (time.monotonic(), value)
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


def cache_key(job: TranslationJob, scope: str, uses_context: bool) -> str:
    """Context-free translations are content-addressed (safe to share: same input, same output).
    Context/glossary-dependent ones are additionally scoped to the chat so nothing leaks between chats."""
    h = hashlib.sha256()
    parts = [normalize_for_key(job.text), job.target, job.style, str(job.html)]
    if uses_context or job.glossary:
        parts.append(scope)
        parts.extend(f"{c.speaker}:{c.text}" for c in job.context)
        if job.reply_to:
            parts.append(f"R:{job.reply_to.text}")
        parts.extend(f"{a}={b}" for a, b in job.glossary)
    h.update("\x1f".join(parts).encode("utf-8"))
    return h.hexdigest()


def validate(job: TranslationJob, res: TranslationResult) -> list[str]:
    """Cheap, local checks — no extra LLM call. Returns a list of problems (empty = OK)."""
    problems: list[str] = []
    if not res.needs_translation:
        return problems
    out = res.translation.strip()
    src_plain = html_to_plain(job.text).strip() if job.html else job.text.strip()
    out_plain = html_to_plain(out).strip() if job.html else out
    if not out_plain:
        problems.append("empty")
        return problems
    if out_plain == src_plain:
        problems.append("identical")
    missing = missing_entities(job.text, out)
    if missing:
        problems.append(f"missing:{len(missing)}")
    if len(src_plain) >= 12 and len(out_plain) > 4 * len(src_plain) + 40:
        problems.append("too_long")  # likely explanation/hallucination
    target = LANGUAGES.get(job.target)
    if target is not None and wrong_script(out_plain, target.script):
        problems.append("wrong_script")
    elif target is not None and romanized_output(src_plain, out_plain, target.script):
        problems.append("romanized")
    elif looks_garbled(src_plain, out_plain, job.target):
        problems.append("garbled")
    low = out_plain.lower()
    if low.startswith(("translation:", "here is", "here's the translation", "sure,", "the translation")):
        problems.append("preamble")
    return problems


class Orchestrator:
    def __init__(self, providers: dict[str, TranslationProvider], metrics: Metrics,
                 *, min_confidence: float = 0.45, provider_timeout: float = 12.0) -> None:
        self.provider_timeout = provider_timeout
        self.providers = providers
        self.metrics = metrics
        self.min_confidence = min_confidence
        self.cache = TTLCache()
        self.breakers = {name: CircuitBreaker() for name in providers}
        self._inflight: dict[str, asyncio.Future[TranslationResult | None]] = {}

    # -- routing ---------------------------------------------------------------------------------
    def route(self, job: TranslationJob, det: Detection | None, purpose: Purpose) -> list[Route]:
        has_g = "gemini" in self.providers
        deepl = self.providers.get("deepl")
        deepl_ok = deepl is not None and deepl.supports(job.source_hint, job.target)
        words = len(html_to_plain(job.text).split()) if job.html else len(job.text.split())
        hard = bool(det and (det.informal or det.romanized or det.mixed or det.verdict is Verdict.UNSURE))
        clean = not hard and words >= 4 and job.style != "ocr"

        routes: list[Route] = []
        if job.style == "ocr":
            if has_g:
                routes.append(Route("gemini", True, "ocr"))
        elif clean and deepl_ok:
            routes.append(Route("deepl", False, "clean"))
        elif has_g:
            needs_quality = bool(
                det and ((det.mixed and det.romanized) or (det.verdict is Verdict.UNSURE and (job.context or job.reply_to)))
            ) and purpose != "inline"
            routes.append(Route("gemini", needs_quality, "hard" if hard else "short"))
        elif deepl_ok and not (det and det.romanized):
            routes.append(Route("deepl", False, "only-provider"))
        # one fallback hop to the other family
        if routes and routes[0].provider == "deepl" and has_g:
            routes.append(Route("gemini", False, "fallback"))
        elif routes and routes[0].provider == "gemini" and deepl_ok and not (det and det.romanized):
            routes.append(Route("deepl", False, "fallback"))
        elif not routes and deepl_ok:
            routes.append(Route("deepl", False, "last-resort"))
        return routes

    # -- main entry ------------------------------------------------------------------------------
    async def translate(self, job: TranslationJob, det: Detection | None, *, purpose: Purpose = "group",
                        scope: str = "", deadline_s: float = 15.0) -> TranslationResult | None:
        """Returns a result with needs_translation=True and a validated translation, or None (stay silent)."""
        uses_context = bool(job.context or job.reply_to)
        key = cache_key(job, scope, uses_context)
        hit = self.cache.get(key)
        if hit is not None:
            self.metrics.inc("cache_hit")
            return hit if hit.needs_translation else None
        self.metrics.inc("cache_miss")
        # coalesce identical concurrent requests (e.g. same sticker text in many chats)
        while (fut := self._inflight.get(key)) is not None:
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise  # we are being cancelled ourselves
                # the owner was cancelled (e.g. its queue job timed out): do the work ourselves
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            res = await self._translate_uncached(job, det, purpose, deadline_s)
            if res is not None:
                self.cache.put(key, res)
            fut.set_result(res if res and res.needs_translation else None)
            return res if res and res.needs_translation else None
        except asyncio.CancelledError:
            fut.cancel()  # waiters retry instead of inheriting our cancellation
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(key, None)

    async def _translate_uncached(self, job: TranslationJob, det: Detection | None, purpose: Purpose,
                                  deadline_s: float) -> TranslationResult | None:
        routes = self.route(job, det, purpose)
        if not routes:
            self.metrics.inc("no_route")
            return None
        deadline = time.monotonic() + deadline_s
        best: TranslationResult | None = None
        best_problems: list[str] = []
        tried_quality_retry = False
        i = 0
        while i < len(routes):
            route = routes[i]
            i += 1
            provider = self.providers[route.provider]
            breaker = self.breakers[route.provider]
            if not breaker.allow():
                self.metrics.inc(f"breaker_open.{route.provider}")
                continue
            res = await self._call(provider, breaker, job, route, deadline, has_fallback=i < len(routes))
            if res is None:
                if i < len(routes):
                    self.metrics.inc("fallback")
                continue
            if not res.needs_translation:
                return res  # provider says: already target / nothing to translate -> silence
            if res.confidence < self.min_confidence:
                self.metrics.inc("low_confidence")
                return replace(res, needs_translation=False, translation="")
            problems = validate(job, res)
            if not problems:
                return res
            self.metrics.inc("validation_fail")
            log.info("validation problems", extra={"problems": problems, "provider": res.provider})
            if "identical" in problems and len(problems) == 1:
                # provider returned the source unchanged: treat as "no translation needed"
                return replace(res, needs_translation=False, translation="")
            if best is None or len(problems) < len(best_problems):
                best, best_problems = res, problems
            # tiered retry: one escalation to the quality model when the fast path produced a bad result
            if (not tried_quality_retry and route.provider == "gemini" and not route.quality
                    and purpose != "inline" and time.monotonic() < deadline - 2):
                tried_quality_retry = True
                routes.insert(i, Route("gemini", True, "validation-retry"))
        if best is not None and best_problems and all(p.startswith("missing") for p in best_problems):
            return best  # minor entity drift is better than silence
        return None

    def _budget(self, remaining: float, has_fallback: bool) -> float:
        """Time this attempt may use. With a fallback queued, leave it a fair share of the deadline
        (observed live: waiting for a free-tier RPM slot consumed the whole deadline -> silent drop)."""
        share = remaining * 0.55 if has_fallback else remaining
        return max(0.5, min(share, self.provider_timeout))

    async def _call(self, provider: TranslationProvider, breaker: CircuitBreaker, job: TranslationJob,
                    route: Route, deadline: float, *, has_fallback: bool = False) -> TranslationResult | None:
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining < 0.5:
                self.metrics.inc("deadline_exceeded")
                return None
            t0 = time.perf_counter()
            try:
                res = await provider.translate(job, quality=route.quality, timeout=self._budget(remaining, has_fallback))
            except ProviderError as err:
                self.metrics.inc(f"provider_error.{err.provider}.{err.kind.value}")
                log.warning("provider error", extra={"provider": err.provider, "kind": err.kind.value,
                                                     "status": err.status, "detail": err.detail})
                if err.kind in (ErrorKind.RATE_LIMITED, ErrorKind.QUOTA):
                    pass  # provider-side limiter state already updated; don't trip breaker for local caps
                elif err.kind is ErrorKind.AUTH:
                    breaker.record_failure(open_for=600.0)
                elif err.kind not in (ErrorKind.BAD_REQUEST, ErrorKind.UNSUPPORTED, ErrorKind.BLOCKED,
                                      ErrorKind.INVALID_OUTPUT):
                    # content-specific outcomes (safety block, truncated JSON for one message) say nothing about
                    # provider health: a few profane messages must not open the breaker for everyone
                    breaker.record_failure()
                if err.kind in RETRYABLE and attempt == 0 and deadline - time.monotonic() > 2.0:
                    await asyncio.sleep(backoff_delay(attempt))
                    continue
                if err.kind not in FALLBACK_OK:
                    return None
                return None
            breaker.record_success()
            self.metrics.observe(f"latency.{res.provider}", (time.perf_counter() - t0) * 1000)
            self.metrics.inc(f"translations.{res.provider}.{res.model}")
            self.metrics.add_usage(res.provider, res.model, res.input_tokens, res.output_tokens, res.billed_chars)
            return res
        return None
