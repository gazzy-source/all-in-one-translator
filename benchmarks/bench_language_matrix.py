"""LIVE language matrix: N source messages x M recipient target languages through the production path.

    python benchmarks/bench_language_matrix.py [--targets en,hi,es,ar,ru,ja] [--pause 1.5]

For every (message, target) it checks automatically:
  * gate        - message already in the target language is skipped; otherwise translated
  * right_lang  - the OUTPUT is detected as the target language by the local gate (TARGET verdict)
  * script      - for non-Latin targets the output's dominant script is the target's native script
  * entities    - URLs/mentions/numbers survived
Outputs are saved for human review in benchmarks/results/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector, Verdict
from translator_bot.languages import LANGUAGES
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import TranslationJob, TranslationProvider
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.textutil import missing_entities, script_counts

SOURCES = [
    ("es", "¿Alguien sabe si mañana estará abierto? 😂"),
    ("en", "Are you coming tonight? Let me know by 8."),
    ("hi", "yaar kya scene hai aaj raat ka?"),
    ("hi", "मुझे नहीं पता कि वह कब आएगा।"),
    ("ar", "مرحبا، كيف حالك؟ اشتقت لك"),
    ("ru", "Где встречаемся завтра? Я буду в 10:00."),
    ("ja", "マジでやばいww 明日どうする？"),
    ("fr", "Grave, c'était ouf mdr, on remet ça samedi ?"),
    ("de", "Ich komme morgen etwas später, fangt ruhig ohne mich an."),
    ("pt", "pq vc não veio ontem? kkkk"),
    ("ko", "ㅋㅋㅋㅋ 오늘 뭐해?"),
    ("en", "Check https://example.com/x and ping @dave before 5pm"),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="en,hi,es,ar,ru,ja")
    ap.add_argument("--pause", type=float, default=1.5)
    args = ap.parse_args()
    targets = args.targets.split(",")
    s = load_settings()
    providers: dict[str, TranslationProvider] = {}
    if s.gemini_api_key:
        providers["gemini"] = GeminiProvider(
            s.gemini_api_key.get_secret_value(), fast_model=s.gemini_fast_model, quality_model=s.gemini_quality_model,
            vision_model=s.gemini_vision_model, fallback_model=s.gemini_fallback_model,
            fast_thinking=s.gemini_fast_thinking, quality_thinking=s.gemini_quality_thinking, rpm=s.gemini_rpm)
    if s.deepl_api_key:
        d = DeepLProvider(s.deepl_api_key.get_secret_value())
        await d.load_languages()
        providers["deepl"] = d
    det = LanguageDetector()
    orch = Orchestrator(providers, Metrics())
    rows = []
    for src_lang, text in SOURCES:
        for tgt in targets:
            d0 = det.detect(text, tgt)
            same = src_lang == tgt
            out = ""
            prov = None
            t0 = time.perf_counter()
            if d0.verdict is not Verdict.TARGET:
                job = TranslationJob(text, tgt, source_hint=d0.lang if d0.verdict is Verdict.OTHER else None,
                                     romanized=d0.romanized, mixed=d0.mixed, unsure=d0.verdict is Verdict.UNSURE)
                res = await orch.translate(job, d0)
                if res:
                    out, prov = res.translation, f"{res.provider}:{res.model}"
                await asyncio.sleep(args.pause)
            ms = round((time.perf_counter() - t0) * 1000)
            row = {"src": src_lang, "tgt": tgt, "text": text, "out": out, "provider": prov, "ms": ms,
                   "gate_ok": (not out) if same else bool(out)}
            if out:
                back = det.detect(out, tgt)
                row["right_lang"] = back.verdict is not Verdict.OTHER
                want = LANGUAGES[tgt].script
                sc = script_counts(out)
                if want != "Latin":
                    dominant = max(sc, key=lambda k: sc[k]) if sc else None
                    row["script_ok"] = dominant == want or (want == "Kana" and dominant in ("Kana", "Han"))
                row["entities_missing"] = missing_entities(text, out)
            rows.append(row)
            flag = "" if row["gate_ok"] and row.get("right_lang", True) and row.get("script_ok", True) else "  <-- CHECK"
            print(f"{src_lang}->{tgt} {prov or '-':28} {ms:5}ms {out[:60]!r}{flag}")
    tr = [r for r in rows if r["out"]]
    summary = {
        "pairs": len(rows),
        "gate_accuracy": round(sum(r["gate_ok"] for r in rows) / len(rows), 3),
        "right_language_rate": round(sum(r["right_lang"] for r in tr) / max(1, len(tr)), 3),
        "native_script_rate": round(sum(r["script_ok"] for r in tr if "script_ok" in r)
                                    / max(1, sum("script_ok" in r for r in tr)), 3),
        "entity_violations": sum(bool(r["entities_missing"]) for r in tr),
        "providers": dict(Counter(r["provider"] for r in tr)),
        "p50_ms": sorted(r["ms"] for r in tr)[len(tr) // 2] if tr else None,
    }
    print(json.dumps(summary, indent=2))
    out_path = Path(__file__).parent / "results" / f"language-matrix-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {out_path}")
    for p in providers.values():
        await p.aclose()


if __name__ == "__main__":
    asyncio.run(main())
