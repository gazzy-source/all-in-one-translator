"""DeepL adapter (REST v2). High-quality NMT for standard text; used for clean messages and as fallback.

Protected spans (URLs, mentions, hashtags, emails, code, glossary "keep" terms) are wrapped in
`<span translate="no">` with tag_handling=html, so DeepL leaves them byte-identical.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import httpx
import regex as re

from translator_bot.providers.base import ErrorKind, ProviderError, TranslationJob, TranslationResult
from translator_bot.resilience import SlidingWindowLimiter
from translator_bot.textutil import CODE_FENCE_RE, EMAIL_RE, HASHTAG_RE, MENTION_RE, URL_RE, html_to_plain

log = logging.getLogger(__name__)

_TARGET_MAP = {"en": "EN-US", "pt": "PT-BR", "zh": "ZH-HANS", "zh-Hant": "ZH-HANT", "nb": "NB"}
_KEEP_OPEN = '<span translate="no">'
_KEEP_RE = re.compile(r'<span translate="no">(.*?)</span>', re.S)
_PROTECT_RE = re.compile("|".join(f"(?:{rx.pattern})" for rx in (CODE_FENCE_RE, URL_RE, EMAIL_RE, MENTION_RE,
                                                                     HASHTAG_RE)), re.S | re.I)


def _protect(text_html: str, keep_terms: list[str]) -> str:
    """Wrap protected spans that appear in text nodes (not inside tags) with translate=no spans."""
    out: list[str] = []
    pattern = _PROTECT_RE
    if keep_terms:
        terms = "|".join(re.escape(t) for t in sorted(keep_terms, key=len, reverse=True))
        pattern = re.compile(f"(?:{_PROTECT_RE.pattern})|(?<![\\p{{L}}])(?:{terms})(?![\\p{{L}}])", re.S | re.I)
    for chunk in re.split(r"(<[^>]+>)", text_html):
        if chunk.startswith("<") and chunk.endswith(">"):
            out.append(chunk)
            continue
        out.append(pattern.sub(lambda m: f"{_KEEP_OPEN}{m.group(0)}</span>", chunk))
    return "".join(out)


def _unprotect(text_html: str) -> str:
    return str(_KEEP_RE.sub(lambda m: m.group(1), text_html))


class DeepLProvider:
    name = "deepl"

    def __init__(self, api_key: str, *, rpm: int = 60, client: httpx.AsyncClient | None = None,
                 base_url: str | None = None, monthly_char_budget: int | None = None) -> None:
        self.monthly_char_budget = monthly_char_budget
        self._key = api_key
        self._base = (base_url or ("https://api-free.deepl.com/v2" if api_key.endswith(":fx")
                                   else "https://api.deepl.com/v2")).rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self._rpm = SlidingWindowLimiter(rpm, 60.0)
        self._sources: set[str] = set()
        self._targets: set[str] = set()
        self.char_count: int | None = None
        self.char_limit: int | None = None
        self.local_chars = 0

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"DeepL-Auth-Key {self._key}"}

    async def load_languages(self) -> None:
        for kind, dest in (("source", self._sources), ("target", self._targets)):
            resp = await self._client.get(f"{self._base}/languages", params={"type": kind},
                                          headers=self._headers(), timeout=10.0)
            if resp.status_code != 200:
                raise self._classify(resp)
            dest.update(str(x["language"]).upper() for x in resp.json())

    async def refresh_usage(self) -> None:
        resp = await self._client.get(f"{self._base}/usage", headers=self._headers(), timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            self.char_count = int(data.get("character_count", 0))
            self.local_chars = 0  # the server count now includes everything we sent
            self.char_limit = int(data.get("character_limit", 0)) or None

    @staticmethod
    def target_code(target: str) -> str:
        return _TARGET_MAP.get(target, target.upper())

    @staticmethod
    def source_code(source: str) -> str:
        return source.split("-")[0].upper()

    def supports(self, source: str | None, target: str) -> bool:
        if not self._targets:
            return False
        tgt = self.target_code(target)
        if tgt not in self._targets and tgt.split("-")[0] not in self._targets:
            return False
        return source is None or self.source_code(source) in self._sources

    def _classify(self, resp: httpx.Response) -> ProviderError:
        s = resp.status_code
        if s == 456:
            return ProviderError(ErrorKind.QUOTA, self.name, "character quota exceeded", status=s)
        if s == 429:
            ra = resp.headers.get("Retry-After")
            return ProviderError(ErrorKind.RATE_LIMITED, self.name, "429", float(ra) if ra and ra.isdigit() else 5.0,
                                 status=s)
        if s in (401, 403):
            return ProviderError(ErrorKind.AUTH, self.name, "auth", status=s)
        if s in (400, 404, 413, 414):
            return ProviderError(ErrorKind.BAD_REQUEST, self.name, f"http {s}", status=s)
        return ProviderError(ErrorKind.TRANSIENT, self.name, f"http {s}", status=s)

    async def translate(self, job: TranslationJob, *, quality: bool, timeout: float) -> TranslationResult:
        if not self.supports(job.source_hint, job.target):
            raise ProviderError(ErrorKind.UNSUPPORTED, self.name, "language pair")
        if self.monthly_char_budget is not None:
            used = (self.char_count or 0) + self.local_chars
            if used + len(job.text) > self.monthly_char_budget:
                raise ProviderError(ErrorKind.QUOTA, self.name, "local monthly character budget reached")
        if not await self._rpm.acquire(max_wait=min(1.0, timeout / 3)):
            raise ProviderError(ErrorKind.RATE_LIMITED, self.name, "local RPM cap", self._rpm.wait_time())
        src_html = job.text if job.html else html.escape(job.text, quote=False)
        keep = [t for t, r in job.glossary if t == r]
        body: dict[str, Any] = {
            "text": [_protect(src_html, keep)],
            "target_lang": self.target_code(job.target),
            "tag_handling": "html",
            "preserve_formatting": True,
        }
        if job.source_hint and self.source_code(job.source_hint) in self._sources:
            body["source_lang"] = self.source_code(job.source_hint)
        ctx = [c.text for c in job.context[-4:]]
        if job.reply_to:
            ctx.append(job.reply_to.text)
        if ctx:
            body["context"] = "\n".join(html_to_plain(c) for c in ctx)[:2000]  # context is not billed
        t0 = time.perf_counter()
        try:
            resp = await self._client.post(f"{self._base}/translate", json=body, headers=self._headers(),
                                           timeout=timeout)
        except httpx.TimeoutException:
            raise ProviderError(ErrorKind.TIMEOUT, self.name, f"timeout after {timeout:.1f}s") from None
        except httpx.HTTPError as exc:
            raise ProviderError(ErrorKind.TRANSIENT, self.name, type(exc).__name__) from None
        if resp.status_code != 200:
            raise self._classify(resp)
        try:
            tr = resp.json()["translations"][0]
            out_html = _unprotect(str(tr["text"]))
            detected = str(tr.get("detected_source_language", "")).lower() or None
            billed = int(tr.get("billed_characters", len(job.text)))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(ErrorKind.INVALID_OUTPUT, self.name, "schema mismatch") from None
        self.local_chars += billed
        translation = out_html if job.html else html_to_plain(out_html)
        same_lang = detected is not None and detected.split("-")[0] == job.target.split("-")[0].lower()
        needs = not same_lang and translation.strip() != html_to_plain(job.text).strip()
        return TranslationResult(
            needs_translation=needs, translation=translation.strip() if needs else "", source_lang=detected,
            confidence=0.85 if needs else 0.9, provider=self.name, model="deepl",
            latency_ms=(time.perf_counter() - t0) * 1000, billed_chars=billed,
        )

    async def health_check(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/usage", headers=self._headers(), timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
