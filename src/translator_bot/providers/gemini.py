"""Google Gemini adapter (REST `generateContent`, structured JSON output, vision).

Uses raw httpx (no SDK) to keep dependencies small. Two model tiers: `fast` (flash-lite) and `quality`.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

from translator_bot.languages import lang_name
from translator_bot.prompts import (
    TRANSLATE_SYSTEM,
    TRANSLATION_SCHEMA,
    VISION_SCHEMA,
    VISION_SYSTEM,
    translation_user_payload,
    vision_user_text,
)
from translator_bot.providers.base import (
    ErrorKind,
    ProviderError,
    TranslationJob,
    TranslationResult,
    VisionResult,
)
from translator_bot.resilience import DailyQuota, SlidingWindowLimiter

log = logging.getLogger(__name__)

_SAFETY = [
    {"category": c, "threshold": "BLOCK_ONLY_HIGH"}
    for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT",
              "HARM_CATEGORY_DANGEROUS_CONTENT")
]
_BLOCK_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY",
                 "IMAGE_PROHIBITED_CONTENT"}


def _parse_duration(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.rstrip("s"))
    except ValueError:
        return None


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        fast_model: str,
        quality_model: str,
        vision_model: str,
        fallback_model: str | None = None,
        fast_thinking: str = "MINIMAL",
        quality_thinking: str = "LOW",
        rpm: int = 10,
        rpd: int = 1000,
        store: bool = False,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self.fast_model, self.quality_model, self.vision_model = fast_model, quality_model, vision_model
        self.fallback_model = fallback_model if fallback_model and fallback_model != fast_model else None
        self.fast_thinking, self.quality_thinking = fast_thinking, quality_thinking
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._store = store
        models = {fast_model, quality_model, vision_model} | ({self.fallback_model} if self.fallback_model else set())
        self._rpm = {m: SlidingWindowLimiter(rpm, 60.0) for m in models}
        self._rpd = {m: DailyQuota(rpd) for m in models}
        # Feature support learned at runtime (some models reject thinkingConfig levels / store).
        self._no_thinking: set[str] = set()
        self._send_store = True

    # -- capability ------------------------------------------------------------------------------
    def supports(self, source: str | None, target: str) -> bool:
        return True  # broad coverage; unknown pairs are attempted

    def available(self, model: str) -> bool:
        return self._rpd[model].remaining() > 0 and self._rpm[model].wait_time() == 0

    def quota_snapshot(self) -> dict[str, dict[str, int]]:
        return {m: {"rpd_remaining": q.remaining(), "rpd_used": q.used} for m, q in self._rpd.items()}

    # -- core request ----------------------------------------------------------------------------
    async def _generate(self, model: str, body: dict[str, Any], timeout: float, max_wait: float) -> dict[str, Any]:
        """Call `model`; on overload (503/local cap) retry once on the fallback model (separate capacity pool)."""
        t0 = time.monotonic()
        try:
            return await self._generate_one(model, body, timeout, max_wait)
        except ProviderError as err:
            # Vision may already run on the fallback model; then fail over to the text model instead.
            alt = self.fallback_model if model != self.fallback_model else self.quality_model
            overloaded = (err.kind is ErrorKind.TRANSIENT and err.status in (500, 503)) or (
                err.kind in (ErrorKind.RATE_LIMITED, ErrorKind.QUOTA))
            remaining = timeout - (time.monotonic() - t0)
            if alt is None or alt == model or not overloaded or remaining < 2.0:
                raise
            self.last_model_used = alt
            return await self._generate_one(alt, body, remaining, max_wait=min(1.0, remaining / 3))

    async def _generate_one(self, model: str, body: dict[str, Any], timeout: float,
                            max_wait: float) -> dict[str, Any]:
        self.last_model_used = model
        if self._rpd[model].remaining() <= 0:
            raise ProviderError(ErrorKind.QUOTA, self.name, f"local daily cap reached for {model}")
        if not await self._rpm[model].acquire(max_wait=max_wait):
            raise ProviderError(ErrorKind.RATE_LIMITED, self.name, f"local RPM cap for {model}",
                                retry_after=self._rpm[model].wait_time())
        self._rpd[model].consume()
        gen = body.setdefault("generationConfig", {})
        if model in self._no_thinking:
            gen.pop("thinkingConfig", None)
        if self._send_store:
            body["store"] = self._store
        url = f"{self._base}/models/{model}:generateContent"
        for attempt in range(2):  # second attempt only after stripping an unsupported field
            try:
                resp = await self._client.post(url, json=body, timeout=timeout,
                                               headers={"x-goog-api-key": self._key})
            except httpx.TimeoutException:
                raise ProviderError(ErrorKind.TIMEOUT, self.name, f"timeout after {timeout:.1f}s") from None
            except httpx.HTTPError as exc:
                raise ProviderError(ErrorKind.TRANSIENT, self.name, type(exc).__name__) from None
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return data
            err = self._classify(resp, model)
            if attempt == 0 and err.kind is ErrorKind.BAD_REQUEST:
                msg = str(err.extra.get("message", "")).lower()
                if "thinking" in msg and "thinkingConfig" in gen:
                    self._no_thinking.add(model)
                    gen.pop("thinkingConfig", None)
                    continue
                if "store" in msg and "store" in body:
                    self._send_store = False
                    body.pop("store", None)
                    continue
            raise err
        raise ProviderError(ErrorKind.BAD_REQUEST, self.name, "request rejected")

    def _classify(self, resp: httpx.Response, model: str) -> ProviderError:
        status = resp.status_code
        try:
            err = resp.json().get("error", {})
        except (ValueError, AttributeError):
            err = {}
        message = str(err.get("message", ""))[:300]
        gstatus = str(err.get("status", ""))
        details = err.get("details", []) or []
        retry_after: float | None = None
        per_day = False
        reason = ""
        for d in details:
            if not isinstance(d, dict):
                continue
            t = str(d.get("@type", ""))
            if t.endswith("RetryInfo"):
                retry_after = _parse_duration(d.get("retryDelay"))
            elif t.endswith("QuotaFailure"):
                for v in d.get("violations", []) or []:
                    if "PerDay" in str(v.get("quotaId", "")) or "per_day" in str(v.get("quotaId", "")).lower():
                        per_day = True
            elif t.endswith("ErrorInfo"):
                reason = str(d.get("reason", ""))
        # message text may echo request data; keep only a safe, short summary in `detail`
        detail = f"{gstatus or status} {reason}".strip()
        extra: dict[str, object] = {"message": message}
        if status == 429:
            if per_day:
                self._rpd[model].exhaust()
                return ProviderError(ErrorKind.QUOTA, self.name, detail, retry_after, status, extra)
            return ProviderError(ErrorKind.RATE_LIMITED, self.name, detail, retry_after or 20.0, status, extra)
        if status in (401, 403) or reason in ("API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED"):
            return ProviderError(ErrorKind.AUTH, self.name, detail, None, status, extra)
        if status in (400, 404, 413, 422):
            if "location is not supported" in message.lower():
                return ProviderError(ErrorKind.AUTH, self.name, "region not supported", None, status, extra)
            return ProviderError(ErrorKind.BAD_REQUEST, self.name, detail, None, status, extra)
        if status == 504:
            return ProviderError(ErrorKind.TIMEOUT, self.name, detail, None, status, extra)
        return ProviderError(ErrorKind.TRANSIENT, self.name, detail, retry_after, status, extra)

    @staticmethod
    def _extract_json(data: dict[str, Any], provider: str) -> tuple[dict[str, Any], int, int]:
        fb = data.get("promptFeedback") or {}
        if fb.get("blockReason"):
            raise ProviderError(ErrorKind.BLOCKED, provider, f"prompt blocked: {fb.get('blockReason')}")
        cands = data.get("candidates") or []
        if not cands:
            raise ProviderError(ErrorKind.INVALID_OUTPUT, provider, "no candidates")
        cand = cands[0]
        finish = str(cand.get("finishReason", ""))
        if finish in _BLOCK_FINISH:
            raise ProviderError(ErrorKind.BLOCKED, provider, f"finish={finish}")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        if finish == "MAX_TOKENS":
            raise ProviderError(ErrorKind.INVALID_OUTPUT, provider, "truncated (MAX_TOKENS)")
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            raise ProviderError(ErrorKind.INVALID_OUTPUT, provider, "non-JSON output") from None
        if not isinstance(obj, dict):
            raise ProviderError(ErrorKind.INVALID_OUTPUT, provider, "JSON is not an object")
        usage = data.get("usageMetadata") or {}
        out_tokens = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
        return obj, int(usage.get("promptTokenCount", 0)), out_tokens

    # -- translation -----------------------------------------------------------------------------
    async def translate(self, job: TranslationJob, *, quality: bool, timeout: float) -> TranslationResult:
        model = self.quality_model if quality else self.fast_model
        est_tokens = max(256, int(len(job.text) * 1.5))
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": TRANSLATE_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": translation_user_payload(job)}]}],
            "safetySettings": _SAFETY,
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": TRANSLATION_SCHEMA,
                "maxOutputTokens": min(8192, est_tokens + 1536),
                "temperature": 0.2,
                "thinkingConfig": {"thinkingLevel": self.quality_thinking if quality else self.fast_thinking},
            },
        }
        t0 = time.perf_counter()
        # Wait for a free-tier RPM slot: briefly on the inline path (short timeout), longer for groups.
        data = await self._generate(model, body, timeout, max_wait=max(0.3, min(8.0, timeout - 4.0)))
        obj, tin, tout = self._extract_json(data, self.name)
        try:
            needs = bool(obj["needs_translation"])
            translation = str(obj.get("translation") or "").strip()
            src = str(obj.get("source_language") or "").strip().lower() or None
            conf = float(obj.get("confidence", 0.0))
        except (KeyError, TypeError, ValueError):
            raise ProviderError(ErrorKind.INVALID_OUTPUT, self.name, "schema mismatch") from None
        if src in ("und", "unknown", ""):
            src = None
        return TranslationResult(
            needs_translation=needs and bool(translation), translation=translation, source_lang=src,
            confidence=max(0.0, min(1.0, conf)), provider=self.name, model=self.last_model_used,
            latency_ms=(time.perf_counter() - t0) * 1000, input_tokens=tin, output_tokens=tout,
        )

    # -- vision ----------------------------------------------------------------------------------
    async def read_images(self, images: list[bytes], targets: list[str], *, animated: bool,
                          caption_hint: str | None, timeout: float, audio: bytes | None = None,
                          audio_mime: str | None = None) -> VisionResult:
        """One call reads text (and speech, if audio is attached) and translates into ALL targets."""
        model = self.vision_model
        parts: list[dict[str, Any]] = [
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(img).decode("ascii")}}
            for img in images
        ]
        if audio:
            parts.append({"inline_data": {"mime_type": audio_mime or "audio/ogg",
                                          "data": base64.b64encode(audio).decode("ascii")}})
        parts.append({"text": vision_user_text(targets, animated=animated, frames=len(images),
                                               caption_hint=caption_hint, audio=bool(audio))})
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": VISION_SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}],
            "safetySettings": _SAFETY,
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": VISION_SCHEMA,
                "maxOutputTokens": 4096 if audio else 2048,
                "temperature": 0.1,
                "thinkingConfig": {"thinkingLevel": "MINIMAL"},
                "mediaResolution": "MEDIA_RESOLUTION_LOW",
            },
        }
        t0 = time.perf_counter()
        data = await self._generate(model, body, timeout, max_wait=3.0)
        obj, tin, tout = self._extract_json(data, self.name)
        try:
            src = str(obj.get("source_language") or "").strip().lower() or None
            wanted = {t.lower(): t for t in targets}
            translations: dict[str, str] = {}
            for item in obj.get("translations") or []:
                lang = str(item.get("language", "")).strip()
                # model may answer "Hindi (hi)", "hi" or "Hindi"
                code = lang.lower().split("(")[-1].rstrip(")").strip() if "(" in lang else lang.lower()
                key = wanted.get(code) or next((t for t in targets if lang_name(t).lower() == lang.lower()), None)
                text = str(item.get("translation") or "").strip()
                if key and text:
                    translations[key] = text
            return VisionResult(
                has_text=bool(obj["has_text"]), text=str(obj.get("text") or "").strip(),
                source_lang=None if src in ("und", "unknown") else src,
                translations=translations,
                confidence=max(0.0, min(1.0, float(obj.get("confidence", 0.0)))),
                provider=self.name, model=self.last_model_used, latency_ms=(time.perf_counter() - t0) * 1000,
                input_tokens=tin, output_tokens=tout,
            )
        except (KeyError, TypeError, ValueError):
            raise ProviderError(ErrorKind.INVALID_OUTPUT, self.name, "schema mismatch") from None

    # -- ops -------------------------------------------------------------------------------------
    async def health_check(self) -> bool:
        try:
            resp = await self._client.get(f"{self._base}/models/{self.fast_model}", timeout=5.0,
                                          headers={"x-goog-api-key": self._key})
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        resp = await self._client.get(f"{self._base}/models", params={"pageSize": 200}, timeout=10.0,
                                      headers={"x-goog-api-key": self._key})
        if resp.status_code != 200:
            raise self._classify(resp, self.fast_model)
        return [str(m.get("name", "")).removeprefix("models/") for m in resp.json().get("models", [])]

    async def aclose(self) -> None:
        await self._client.aclose()
