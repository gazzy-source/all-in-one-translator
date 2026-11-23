"""Real providers + simulated Telegram accounts through the complete group pipeline.

Run: python -m benchmarks.bench_group_simulation --live
Eight source languages / eight reader languages; 13 fake users, 63 expected deliveries,
56 distinct translation pairs. No real Telegram token, users, group, or database is used.
Reads provider keys from .env. Paces rounds to respect the configured Gemini RPM.
Stores only validation results/hashes, never message or translation text.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path

from tests.conftest import group_message
from tests.test_group_matrix import ACTIVE, LEFT, MUTED, PAUSED, READERS, TEXTS, UNAPPROVED, matrix_group

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector
from translator_bot.orchestrator import validate
from translator_bot.providers.base import TranslationJob, TranslationProvider, TranslationResult
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.textutil import html_to_plain


async def run(sources: list[str], output: Path) -> bool:
    settings = load_settings()
    providers: dict[str, TranslationProvider] = {}
    try:
        if settings.gemini_api_key:
            providers["gemini"] = GeminiProvider(
                settings.gemini_api_key.get_secret_value(), fast_model=settings.gemini_fast_model,
                quality_model=settings.gemini_quality_model, vision_model=settings.gemini_vision_model,
                fallback_model=settings.gemini_fallback_model, fast_thinking=settings.gemini_fast_thinking,
                quality_thinking=settings.gemini_quality_thinking, rpm=settings.gemini_rpm,
                rpd=settings.gemini_rpd, store=settings.gemini_store, hedge_s=settings.gemini_hedge_s,
                base_url=settings.gemini_base_url)
        if settings.deepl_api_key:
            deepl = DeepLProvider(settings.deepl_api_key.get_secret_value(), rpm=settings.deepl_rpm,
                                  monthly_char_budget=settings.deepl_monthly_char_budget)
            providers["deepl"] = deepl
            await deepl.load_languages()
            await deepl.refresh_usage()
        if not providers:
            raise RuntimeError("No configured translation providers")
        detector = LanguageDetector(wordlist="small")  # deployed VPS configuration
        results = []
        delivered = expected = unexpected = 0
        interval = 60 * (len(TEXTS) - 1) / settings.gemini_rpm + 2 if "gemini" in providers else 0
        with tempfile.TemporaryDirectory(prefix="translator-group-simulation-") as directory:
            async with matrix_group(Path(directory), detector, providers) as h:
                for index, source in enumerate(sources):
                    start = time.monotonic()
                    h.tg.calls.clear()
                    await h.send(group_message(TEXTS[source], sender=READERS[source]))
                    await h.idle(timeout=90)
                    cases = []
                    for uid, target in ACTIVE.items():
                        sends = h.tg.ephemeral_to(uid)
                        needed = target != source
                        expected += int(needed)
                        delivered += len(sends) if needed else 0
                        unexpected += len(sends) if not needed else 0
                        problems = []
                        if len(sends) != int(needed):
                            problems.append("delivery_count")
                        output_hash = None
                        if needed and sends:
                            text = html_to_plain(sends[0].params["text"])
                            output_hash = hashlib.sha256(text.encode()).hexdigest()
                            problems.extend(validate(TranslationJob(TEXTS[source], target),
                                                     TranslationResult(True, text, source, 1.0, "simulation", "")))
                        cases.append({"receiver": uid, "target": target, "expected": needed,
                                      "received": len(sends), "problems": problems, "hash": output_hash})
                    unauthorized = sum(len(h.tg.ephemeral_to(uid)) + len(h.tg.dms_to(uid))
                                       for uid in (UNAPPROVED, PAUSED, MUTED, LEFT))
                    unexpected += unauthorized
                    results.append({"source": source, "elapsed_s": round(time.monotonic() - start, 2),
                                    "unauthorized_deliveries": unauthorized, "cases": cases})
                    print(json.dumps({"source": source, "passed": sum(not c["problems"] for c in cases),
                                      "checked": len(cases), "unauthorized_deliveries": unauthorized,
                                      "failures": [c for c in cases if c["problems"]],
                                      "counters": dict(h.app.metrics.counters)}), flush=True)
                    assert not h.tg.public_group_output() and h.app.guard.blocked == 0
                    if index + 1 < len(sources):
                        await asyncio.sleep(max(0, interval - (time.monotonic() - start)))
                summary = {"mode": "real providers; simulated Telegram", "fake_users": 13,
                           "languages": list(TEXTS), "source_rounds": len(sources),
                           "expected_deliveries": expected, "received_deliveries": delivered,
                           "unexpected_deliveries": unexpected, "public_group_output": 0,
                           "failed_checks": sum(bool(c["problems"]) for r in results for c in r["cases"]),
                           "provider_usage": dict(h.app.metrics.usage),
                           "metrics": h.app.metrics.snapshot(), "results": results}
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                print(json.dumps({k: v for k, v in summary.items()
                                  if k not in ("results", "provider_usage", "metrics")}), flush=True)
                print(f"Report: {output}", flush=True)
                return not summary["failed_checks"] and not unexpected
    finally:
        await asyncio.gather(*(provider.aclose() for provider in providers.values()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Confirm use of real provider quota")
    parser.add_argument("--sources", default=",".join(TEXTS))
    parser.add_argument("--output", type=Path,
                        default=Path("benchmarks/results") / f"group-simulation-{time.strftime('%Y%m%d-%H%M%S')}.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to spend provider quota; Telegram remains simulated")
    source_codes = args.sources.split(",")
    if not source_codes or any(source not in TEXTS for source in source_codes):
        parser.error("Unknown source language")
    raise SystemExit(0 if asyncio.run(run(source_codes, args.output)) else 1)
