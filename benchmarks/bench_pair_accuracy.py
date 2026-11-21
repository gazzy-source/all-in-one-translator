"""LIVE meaning accuracy across language pairs (16 sources x 8 targets), judged independently of Gemini.

    python benchmarks/bench_pair_accuracy.py [--targets en,es,hi,ar,ru,ja,zh,fr] [--pause 1]

Every source has an English meaning written before any output was seen. Each translation is back-translated to
English with DeepL (auto-detected source) and scored with chrF against that meaning; English-target outputs are
scored directly. Back-translation adds its own noise, so low scores are flagged for a human read rather than
treated as verdicts. Also checks native script, romanization, garbling and emoji/number preservation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.bench_translation import chrf

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector, Verdict
from translator_bot.languages import LANGUAGES
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import TranslationJob, TranslationProvider
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.textutil import looks_garbled, missing_entities, romanized_output, wrong_script

# (source language, text, English meaning) — written before looking at any output
SOURCES = [
    ("es", "Oye, ¿me pasas el link del grupo? Se me borró el chat 😅",
     "Hey, can you send me the group link? My chat got deleted 😅"),
    ("en", "I'm running 15 minutes late, save me a seat pls 🙏",
     "I'm running 15 minutes late, save me a seat please 🙏"),
    ("hi", "कल की मीटिंग कैंसल हो गई है, सबको बता देना।", "Tomorrow's meeting has been cancelled, tell everyone."),
    ("hi", "bhai aaj match dekhne chalega kya?", "Bro, are you coming to watch the match today?"),
    ("ar", "لا تنسوا تجيبون الأكل معكم بكرة", "Don't forget to bring the food with you tomorrow."),
    ("ru", "Ну ты даёшь! Как ты успел всё сделать за один день?",
     "Wow, you're something! How did you manage to do everything in one day?"),
    ("ja", "今日の夜、駅前のカフェで会おうよ", "Let's meet at the café in front of the station tonight."),
    ("zh", "这个周末有人想去爬山吗？天气好像不错", "Does anyone want to go hiking this weekend? The weather looks nice."),
    ("ko", "배고파 죽겠다 ㅠㅠ 점심 뭐 먹을래?", "I'm starving TT What do you want for lunch?"),
    ("tr", "Yarın sınav var, hiç çalışmadım, yandım 😭", "There's an exam tomorrow, I haven't studied at all, I'm done for 😭"),
    ("fr", "T'inquiète, je m'en occupe, c'est pas la mer à boire.",
     "Don't worry, I'll take care of it, it's not a big deal."),
    ("pt", "Gente, o show foi cancelado, vão devolver o dinheiro",
     "Guys, the concert was cancelled, they'll refund the money."),
    ("de", "Hat jemand ein Ladekabel für ein iPhone dabei?", "Does anyone have an iPhone charging cable with them?"),
    ("id", "Maaf telat bales, tadi lagi di jalan", "Sorry for the late reply, I was on the road."),
    ("vi", "Mai mình đi ăn phở nha, mình bao!", "Let's go eat pho tomorrow, my treat!"),
    ("fa", "ببخشید دیر کردم، ترافیک خیلی سنگین بود", "Sorry I'm late, the traffic was really heavy."),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="en,es,hi,ar,ru,ja,zh,fr")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()
    targets = args.targets.split(",")
    s = load_settings()
    assert s.gemini_api_key and s.deepl_api_key, "needs GEMINI_API_KEY and DEEPL_API_KEY"
    g = GeminiProvider(s.gemini_api_key.get_secret_value(), fast_model=s.gemini_fast_model,
                       quality_model=s.gemini_quality_model, vision_model=s.gemini_vision_model,
                       fallback_model=s.gemini_fallback_model, fast_thinking=s.gemini_fast_thinking,
                       quality_thinking=s.gemini_quality_thinking, rpm=s.gemini_rpm)
    dl = DeepLProvider(s.deepl_api_key.get_secret_value())
    await dl.load_languages()
    providers: dict[str, TranslationProvider] = {"gemini": g, "deepl": dl}
    orch = Orchestrator(providers, Metrics())
    det = LanguageDetector()
    rows: list[dict[str, Any]] = []
    for src, text, meaning in SOURCES:
        for tgt in targets:
            if src == tgt:
                continue
            d = det.detect(text, tgt)
            row: dict[str, Any] = {"src": src, "tgt": tgt, "text": text, "meaning": meaning}
            t0 = time.perf_counter()
            res = None
            if d.verdict is not Verdict.TARGET:
                job = TranslationJob(text, tgt, source_hint=d.lang if d.verdict is Verdict.OTHER else None,
                                     romanized=d.romanized, mixed=d.mixed, unsure=d.verdict is Verdict.UNSURE)
                try:
                    res = await orch.translate(job, d)
                except Exception as e:  # record and continue
                    row["error"] = repr(e)
            out = res.translation if res and res.needs_translation else ""
            row.update(out=out, provider=f"{res.provider}:{res.model}" if res else None,
                       ms=round((time.perf_counter() - t0) * 1000), gate=d.verdict.value)
            if out:
                script = LANGUAGES[tgt].script
                row["script_ok"] = not wrong_script(out, script) and not romanized_output(text, out, script)
                row["garbled"] = looks_garbled(text, out, tgt)
                row["entities_missing"] = missing_entities(text, out)
                if tgt == "en":
                    back = out
                else:
                    try:
                        back = (await dl.translate(TranslationJob(out, "en"), quality=False, timeout=15)).translation
                    except Exception as e:
                        back, row["back_error"] = "", repr(e)[:80]
                row["back_en"] = back
                row["chrf_meaning"] = round(chrf(back, meaning), 1) if back else None
            rows.append(row)
            print(f"{src}->{tgt} {row['provider'] or '-':30} {row['ms']:5}ms chrF={row.get('chrf_meaning')!s:5} "
                  f"{out[:55]!r}", flush=True)
            await asyncio.sleep(args.pause)
    done = [r for r in rows if r.get("out")]
    scored = [r for r in done if r.get("chrf_meaning") is not None]
    summary = {
        "pairs": len(rows), "translated": len(done), "silent": len(rows) - len(done),
        "errors": sum("error" in r for r in rows),
        "mean_meaning_chrf": round(statistics.mean(r["chrf_meaning"] for r in scored), 1) if scored else None,
        "by_target": {t: round(statistics.mean(r["chrf_meaning"] for r in scored if r["tgt"] == t), 1)
                      for t in targets if any(r["tgt"] == t for r in scored)},
        "by_source": {sl: round(statistics.mean(r["chrf_meaning"] for r in scored if r["src"] == sl), 1)
                      for sl in dict.fromkeys(x[0] for x in SOURCES) if any(r["src"] == sl for r in scored)},
        "script_problems": sum(not r["script_ok"] for r in done), "garbled": sum(r["garbled"] for r in done),
        "entity_drops": sum(bool(r["entities_missing"]) for r in done),
        "providers": {p: sum(r["provider"] == p for r in done) for p in {r["provider"] for r in done}},
        "p50_ms": sorted(r["ms"] for r in done)[len(done) // 2] if done else None,
    }
    print("\n--- lowest meaning scores (read these) ---")
    for r in sorted(scored, key=lambda r: r["chrf_meaning"])[:20]:
        print(f"{r['chrf_meaning']:5} {r['src']}->{r['tgt']} {r['text'][:40]!r}\n      out:  {r['out']!r}\n"
              f"      back: {r['back_en']!r}\n      want: {r['meaning']!r}")
    for r in rows:
        if not r.get("out"):
            print(f"SILENT {r['src']}->{r['tgt']} gate={r['gate']} {r.get('error', '')}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    path = Path(__file__).parent / "results" / f"pair-accuracy-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {path}")
    await g.aclose()


if __name__ == "__main__":
    asyncio.run(main())
