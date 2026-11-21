"""Offline benchmark for the language gate. Run: python benchmarks/bench_detection.py [-v]

Metrics
  false_translate: skip-cases the gate marked OTHER  (bot would post an unwanted translation)
  missed:          translate-cases the gate marked TARGET (bot stays silent when it shouldn't)
  unsure_rate:     cases deferred to the provider (costs a provider call; provider decides)
  lang_acc:        source-language accuracy over translate-cases where the gate committed to a language
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from translator_bot.detect import LanguageDetector, Verdict


def run(verbose: bool = False) -> dict[str, float]:
    cases = json.loads((Path(__file__).parent / "detection_corpus.json").read_text(encoding="utf-8"))
    det = LanguageDetector()
    fp = missed = unsure = lang_ok = lang_n = 0
    skips = sum(1 for c in cases if c["expect"] == "skip")
    trans = sum(1 for c in cases if c["expect"] == "translate")
    t0 = time.perf_counter()
    for c in cases:
        d = det.detect(c["text"], c["target"])
        bad = ""
        if d.verdict is Verdict.UNSURE:
            unsure += 1
        if c["expect"] == "skip" and d.verdict is Verdict.OTHER:
            fp += 1
            bad = "FALSE-TRANSLATE"
        if c["expect"] == "translate" and d.verdict is Verdict.TARGET:
            missed += 1
            bad = "MISSED"
        if c["expect"] == "translate" and c.get("lang") and d.verdict is Verdict.OTHER and d.lang:
            lang_n += 1
            if d.lang.split("-")[0] == c["lang"]:
                lang_ok += 1
            else:
                bad = bad or f"LANG({d.lang}!={c['lang']})"
        if verbose or bad:
            print(f"{bad:18} {c['target']} {d.verdict.value:6} {d.lang!s:5} {d.confidence:.2f} "
                  f"rom={d.romanized:d} mix={d.mixed:d} {c['text']!r} {d.signals}")
    elapsed = (time.perf_counter() - t0) * 1000 / len(cases)
    res = {
        "cases": len(cases),
        "false_translate_rate": fp / skips,
        "missed_rate": missed / trans,
        "unsure_rate": unsure / len(cases),
        "lang_acc": lang_ok / lang_n if lang_n else 0.0,
        "ms_per_msg": elapsed,
    }
    print(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    run("-v" in sys.argv)
