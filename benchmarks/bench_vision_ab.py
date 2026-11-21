"""LIVE A/B of the vision model on real stickers, problem stickers and non-English voice (edge-tts, optional).

    python benchmarks/bench_vision_ab.py   # needs data/real_stickers/meta.json (downloaded via getStickerSet)
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.bench_media import Bot
from benchmarks.bench_media_matrix import render, sticker_html, webp

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector
from translator_bot.media.processor import MediaProcessor
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import TranslationJob
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.storage import Storage

TARGETS = ["en", "hi", "ru", "ar"]
PROBLEMS = [("st-el", "ΕΛΑ ΡΕ!", "🙄", "come on, man!"), ("st-id", "WKWKWK GAS TERUS", "😆", "lol keep going"),
            ("st-hinglish", "BHAI KYA SCENE HAI", "😏", "bro what's up"), ("st-ta", "சூப்பர் மச்சான்", "😎", "super, buddy"),
            ("st-th", "555 ตลกมาก", "🤣", "lol so funny"), ("st-fr", "MDR T'ES SÉRIEUX ?", "😂", "lol are you serious?"),
            ("st-ja-vert", "がんばって", "🐱", "do your best")]
VOICES = [("voice-es", "es-MX-JorgeNeural", "Oye, ya voy en camino, llego en diez minutos, no empiecen sin mí."),
          ("voice-hi", "hi-IN-MadhurNeural", "यार मैं ट्रैफिक में फंसा हूँ, तुम लोग शुरू कर दो।"),
          ("voice-ar", "ar-EG-ShakirNeural", "يا جماعة أنا جاي في الطريق، استنوني شوية."),
          ("voice-ru", "ru-RU-DmitryNeural", "Ребята, я немного опоздаю, начинайте без меня."),
          ("voice-ja", "ja-JP-KeitaNeural", "ごめん、ちょっと遅れる！先に始めてて。"),
          ("voice-pt", "pt-BR-AntonioNeural", "Galera, tô chegando, me esperem aí rapidinho.")]


async def tts(voice: str, text: str) -> bytes | None:
    try:
        import edge_tts
    except ImportError:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        mp3, ogg = Path(tmp) / "a.mp3", Path(tmp) / "a.ogg"
        await edge_tts.Communicate(text, voice).save(str(mp3))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-c:a", "libopus", "-b:a", "24k",
                        "-ac", "1", str(ogg)], check=True)
        return ogg.read_bytes()


async def items() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    meta = Path("data/real_stickers/meta.json")
    if meta.exists():
        for m in json.loads(meta.read_text("utf-8")):
            out.append({"id": m["id"], "kind": m["kind"], "data": Path(m["path"]).read_bytes(), "meaning": "?"})
    loop = asyncio.get_running_loop()
    for id_, text, emoji, meaning in PROBLEMS:
        extra = {"extra": ""} if id_ != "st-ja-vert" else {"extra": "writing-mode:vertical-rl;width:auto;left:190px"}
        png = await loop.run_in_executor(None, lambda t=text, e=emoji, x=extra: render(sticker_html(t, e, **x)))
        out.append({"id": id_, "kind": "sticker", "data": webp(png), "meaning": meaning})
    for id_, voice, text in VOICES:
        data = await tts(voice, text)
        if data:
            out.append({"id": id_, "kind": "voice", "data": data, "meaning": text, "duration": 6})
    return out


async def arm(name: str, vision_model: str, its: list[dict[str, Any]], s: Any, orch: Orchestrator,
              det: LanguageDetector) -> dict[str, Any]:
    g = GeminiProvider(s.gemini_api_key.get_secret_value(), fast_model=vision_model, quality_model=vision_model,
                       vision_model=vision_model, fallback_model=None, rpm=s.gemini_rpm)
    res: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        st = Storage(Path(tmp) / "m.sqlite3")
        await st.open()
        proc = MediaProcessor(Bot(b""), st, Metrics(), vision=g, max_download_bytes=8_000_000,  # type: ignore[arg-type]
                              max_image_pixels=25_000_000, max_tgs_json_bytes=4_000_000, max_video_seconds=15,
                              max_frames=24, ffmpeg_path="ffmpeg", timeout_s=60, workers=1, ocr_min_confidence=0.6,
                              cache_ttl_s=86400)
        for it in its:
            proc.bot = Bot(it["data"])  # type: ignore[assignment]
            t = time.perf_counter()
            try:
                r = await proc.extract(file_id="f", file_unique_id=f"{name}-{it['id']}", size=len(it["data"]),
                                       kind=it["kind"], targets=TARGETS, duration=it.get("duration"))
            except Exception as e:  # record and continue
                res[it["id"]] = {"error": repr(e)}
                continue
            row: dict[str, Any] = {"ms": round((time.perf_counter() - t) * 1000), "found": bool(r),
                                   "read": r.text if r else "", "src": r.source_lang if r else None, "out": {}}
            if r:
                for tgt in TARGETS:
                    tr = r.translations.get(tgt)
                    if tr is None and (r.source_lang or "") != tgt:  # dropped -> text translator, like the pipeline
                        x = await orch.translate(TranslationJob(r.text, tgt, style="ocr"), det.detect(r.text, tgt),
                                                 purpose="ocr")
                        tr = ("↻ " + x.translation) if x and x.needs_translation else "↻ "
                    row["out"][tgt] = tr or ""
            res[it["id"]] = row
        await proc.close()
        await st.close()
    await g.aclose()
    return res


async def main() -> None:
    s = load_settings()
    t0 = time.perf_counter()
    its = await items()
    print(f"{len(its)} items ready in {time.perf_counter() - t0:.0f}s", flush=True)
    d = DeepLProvider(s.deepl_api_key.get_secret_value())
    await d.load_languages()
    g_text = GeminiProvider(s.gemini_api_key.get_secret_value(), fast_model=s.gemini_fast_model,
                            quality_model=s.gemini_quality_model, vision_model=s.gemini_vision_model,
                            fallback_model=s.gemini_fallback_model, rpm=s.gemini_rpm)
    orch = Orchestrator({"gemini": g_text, "deepl": d}, Metrics())
    det = LanguageDetector()
    a, b = await asyncio.gather(arm("A35", "gemini-3.5-flash-lite", its, s, orch, det),
                                arm("B31", "gemini-3.1-flash-lite", its, s, orch, det))
    for it in its:
        print(f"\n[{it['id']}] {it['kind']}  meaning: {it['meaning']}")
        for name, r in (("3.5", a[it["id"]]), ("3.1", b[it["id"]])):
            if "error" in r:
                print(f"  {name}: ERROR {r['error'][:120]}")
                continue
            print(f"  {name} {r['ms']}ms read={r['read']!r} src={r['src']}")
            for tgt, tr in r["out"].items():
                print(f"      {tgt}: {tr!r}")
    path = Path(__file__).parent / "results" / f"vision-ab-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"A35": a, "B31": b}, ensure_ascii=False, indent=1), "utf-8")
    print(f"\ntotal {time.perf_counter() - t0:.0f}s; wrote {path}")
    await g_text.aclose()


if __name__ == "__main__":
    asyncio.run(main())
