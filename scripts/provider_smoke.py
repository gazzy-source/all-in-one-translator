"""LIVE provider smoke test: model availability, one call per path, DeepL languages/usage.

    python scripts/provider_smoke.py
Prints sanitized results only (no keys). Spends ~4 Gemini requests and ~100 DeepL characters.
"""

from __future__ import annotations

import asyncio
import json

from translator_bot.config import load_settings
from translator_bot.providers.base import ContextLine, ProviderError, TranslationJob
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider


async def main() -> None:
    s = load_settings()
    report: dict[str, object] = {}
    if s.gemini_api_key:
        g = GeminiProvider(s.gemini_api_key.get_secret_value(), fast_model=s.gemini_fast_model,
                           quality_model=s.gemini_quality_model, vision_model=s.gemini_vision_model,
                                             fallback_model=s.gemini_fallback_model,
                                             fast_thinking=s.gemini_fast_thinking,
                                             quality_thinking=s.gemini_quality_thinking,
                           rpm=s.gemini_rpm, rpd=s.gemini_rpd, store=s.gemini_store)
        try:
            models = await g.list_models()
            report["gemini_models_matching"] = sorted(m for m in models if "flash" in m or "gemini-4" in m)
            report["configured_available"] = {m: m in models for m in
                                              {s.gemini_fast_model, s.gemini_quality_model, s.gemini_vision_model}}
        except ProviderError as e:
            report["gemini_list_error"] = str(e)
        for quality in (False, True):
            try:
                r = await g.translate(TranslationJob("ni modo", "en", context=(
                    ContextLine("B", "Are you coming tonight?"), ContextLine("A", "no puedo, tengo que trabajar"))),
                    quality=quality, timeout=30)
                report[f"gemini_{'quality' if quality else 'fast'}"] = {
                    "out": r.translation, "src": r.source_lang, "conf": r.confidence, "ms": round(r.latency_ms),
                    "tokens": [r.input_tokens, r.output_tokens], "model": r.model}
            except ProviderError as e:
                report[f"gemini_{'quality' if quality else 'fast'}_error"] = {"err": str(e), "msg": e.extra.get("message")}
        try:
            import sys
            sys.path.insert(0, ".")
            from tests.fixtures import text_image

            from translator_bot.media.frames import load_image

            img = load_image(text_image("NO WAY BRO", transparent=True, fmt="WEBP"), 25_000_000).frames[0]
            v = await g.read_images([img], "es", animated=False, caption_hint=None, timeout=30)
            report["gemini_vision"] = {"has_text": v.has_text, "text": v.text, "tr": v.translation,
                                       "conf": v.confidence, "ms": round(v.latency_ms), "tokens": [v.input_tokens,
                                                                                                    v.output_tokens]}
        except ProviderError as e:
            report["gemini_vision_error"] = {"err": str(e), "msg": e.extra.get("message")}
        report["gemini_learned"] = {"no_thinking": sorted(g._no_thinking), "send_store": g._send_store}
        await g.aclose()
    if s.deepl_api_key:
        d = DeepLProvider(s.deepl_api_key.get_secret_value())
        try:
            await d.load_languages()
            await d.refresh_usage()
            report["deepl_targets"] = sorted(d._targets)
            report["deepl_hi_target"] = d.supports(None, "hi")
            report["deepl_usage"] = [d.char_count, d.char_limit]
            r = await d.translate(TranslationJob("Mira https://x.io @ana, nos vemos a las 18:30.", "en"),
                                  quality=False, timeout=20)
            report["deepl_call"] = {"out": r.translation, "src": r.source_lang, "ms": round(r.latency_ms),
                                    "billed": r.billed_chars}
        except ProviderError as e:
            report["deepl_error"] = str(e)
        await d.aclose()
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
