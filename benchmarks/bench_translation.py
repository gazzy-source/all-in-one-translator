"""LIVE translation benchmark (uses real provider keys from .env; spends free-tier quota).

    python benchmarks/bench_translation.py [--route auto|gemini-fast|gemini-quality|deepl] [--limit N]

Per case it runs the production path (detector gate -> orchestrator routing -> provider -> validation) and
records: output, provider/model, latency, chrF vs reference, entity/keep checks, false-translation for skip
cases. Results are written to benchmarks/results/<timestamp>.json for HUMAN REVIEW — chrF is only a rough
signal (idiomatic translations legitimately differ from the reference); no LLM-judged score is reported as truth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector, Verdict
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import ContextLine, TranslationJob, TranslationProvider
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.textutil import missing_entities

HERE = Path(__file__).parent


def chrf(hyp: str, ref: str, n: int = 6, beta: float = 2.0) -> float:
    """Character n-gram F-score (chrF, Popović 2015), whitespace-insensitive, 0..100."""
    def grams(s: str, k: int) -> Counter[str]:
        s = re.sub(r"\s+", "", s.lower())
        return Counter(s[i:i + k] for i in range(len(s) - k + 1))

    precs, recs = [], []
    for k in range(1, n + 1):
        h, r = grams(hyp, k), grams(ref, k)
        if not h or not r:
            continue
        overlap = sum((h & r).values())
        precs.append(overlap / sum(h.values()))
        recs.append(overlap / sum(r.values()))
    if not precs:
        return 0.0
    p, r = statistics.mean(precs), statistics.mean(recs)
    if p + r == 0:
        return 0.0
    return 100 * (1 + beta**2) * p * r / (beta**2 * p + r)


def keep_ok(out: str, keep: list[str]) -> list[str]:
    missing = []
    for k in keep:
        if not any(alt in out for alt in k.split("|")):
            missing.append(k)
    return missing


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", default="auto", choices=["auto", "gemini-fast", "gemini-quality", "deepl"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--corpus", default="translation_corpus.json", help="file in benchmarks/")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between cases (free-tier RPM)")
    args = ap.parse_args()
    s = load_settings()
    providers: dict[str, TranslationProvider] = {}
    if s.gemini_api_key:
        providers["gemini"] = GeminiProvider(s.gemini_api_key.get_secret_value(), fast_model=s.gemini_fast_model,
                                             quality_model=s.gemini_quality_model, vision_model=s.gemini_vision_model,
                                             fallback_model=s.gemini_fallback_model,
                                             fast_thinking=s.gemini_fast_thinking,
                                             quality_thinking=s.gemini_quality_thinking,
                                             rpm=s.gemini_rpm, rpd=s.gemini_rpd, store=s.gemini_store)
    if s.deepl_api_key:
        d = DeepLProvider(s.deepl_api_key.get_secret_value())
        await d.load_languages()
        providers["deepl"] = d
    det = LanguageDetector()
    orch = Orchestrator(providers, Metrics())
    cases = json.loads((HERE / args.corpus).read_text("utf-8"))
    if args.limit:
        cases = cases[: args.limit]
    rows: list[dict[str, Any]] = []
    for c in cases:
        ctx = tuple(ContextLine("B" if i % 2 == 0 else "C", t) for i, t in enumerate(c.get("context", [])))
        d = det.detect(c["text"], c["target"])
        job = TranslationJob(c["text"], c["target"], source_hint=d.lang if d.verdict is Verdict.OTHER else None,
                             context=ctx, style=c.get("style", "chat"), romanized=d.romanized, mixed=d.mixed,
                             unsure=d.verdict is Verdict.UNSURE)
        t0 = time.perf_counter()
        res = None
        error = None
        if d.verdict is not Verdict.TARGET:
            try:
                if args.route == "auto":
                    res = await orch.translate(job, d, purpose="ocr" if job.style == "ocr" else "group")
                else:
                    name, _, tier = args.route.partition("-")
                    res = await providers[name].translate(job, quality=tier == "quality", timeout=20)
                    if not res.needs_translation:
                        res = None
            except Exception as exc:  # benchmark: record, don't crash
                error = f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - t0) * 1000
        out = res.translation if res else ""
        row: dict[str, Any] = {"id": c["id"], "tags": c.get("tags", []), "text": c["text"], "target": c["target"],
                               "gate": d.verdict.value, "output": out, "provider": res.provider if res else None,
                               "model": res.model if res else None, "src_pred": res.source_lang if res else d.lang,
                               "ms": round(ms), "error": error}
        if c.get("expect_skip"):
            row["false_translation"] = bool(out)
        else:
            row["silent_miss"] = not out
            row["chrf"] = round(chrf(out, c["ref"]), 1) if out else 0.0
            row["keep_missing"] = keep_ok(out, c.get("keep", [])) if out else c.get("keep", [])
            row["entity_missing"] = missing_entities(c["text"], out) if out else []
            row["src_ok"] = (row["src_pred"] or "").split("-")[0] == c.get("src") if c.get("src") else None
            row["ref"] = c["ref"]
            for must in c.get("must_contain", []):
                row.setdefault("must_missing", [])
                if must.lower() not in out.lower():
                    row["must_missing"].append(must)
        rows.append(row)
        flag = "" if not row.get("error") else " ERROR"
        print(f"{row['id']:16} {row['gate']:6} {row.get('provider') or '-':7} {row['ms']:5}ms "
              f"chrF={row.get('chrf', '-')!s:5} {out[:70]!r}{flag}")
        if args.pause:
            await asyncio.sleep(args.pause)
    tr = [r for r in rows if "chrf" in r]
    sk = [r for r in rows if "false_translation" in r]
    summary = {
        "route": args.route, "cases": len(rows),
        "mean_chrf": round(statistics.mean(r["chrf"] for r in tr), 1) if tr else None,
        "silent_miss_rate": round(sum(r["silent_miss"] for r in tr) / max(1, len(tr)), 3),
        "keep_violation_rate": round(sum(bool(r["keep_missing"]) for r in tr if not r["silent_miss"]) /
                                     max(1, sum(not r["silent_miss"] for r in tr)), 3),
        "source_lang_acc": round(sum(bool(r["src_ok"]) for r in tr if r["src_ok"] is not None) /
                                 max(1, sum(r["src_ok"] is not None for r in tr)), 3),
        "false_translation_rate": round(sum(r["false_translation"] for r in sk) / max(1, len(sk)), 3),
        "errors": sum(bool(r["error"]) for r in rows),
        "latency_ms_p50": statistics.median(r["ms"] for r in rows if r["provider"]) if any(r["provider"] for r in rows) else None,
        "providers": dict(Counter(f"{r['provider']}:{r['model']}" for r in rows if r["provider"])),
        "by_tag_chrf": {t: round(statistics.mean(r["chrf"] for r in tr if t in r["tags"]), 1)
                        for t in sorted({t for r in tr for t in r["tags"]})},
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    out_path = HERE / "results" / f"translation-{Path(args.corpus).stem}-{args.route}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False), "utf-8")
    print(f"wrote {out_path}")
    for p in providers.values():
        await p.aclose()


if __name__ == "__main__":
    asyncio.run(main())
