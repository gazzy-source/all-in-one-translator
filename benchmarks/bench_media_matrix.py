"""LIVE media matrix: many scripts, sticker styles and media types, scored for *correctness*, not just detection.

    python benchmarks/bench_media_matrix.py [--targets en,hi,ru,ar] [--pause 1] [--only st-ar,gif-]

Fixtures are rendered by headless Chrome (real text shaping for Arabic/Persian/Indic/Thai, colour emoji, outlined
sticker text) — Pillow without raqm can't shape complex scripts. Speech uses Windows SAPI (English voices only).

Cases are written with their expected source text, language and English meaning *before* looking at outputs. Per
item: text found vs expected, similarity of the recognised text, source language, and per target language: right
script, not garbled, and a DeepL back-translation to English (an independent check of the meaning — Gemini is not
used to judge Gemini). Targets the media call drops (wrong script / garbled) are redone by the text translator,
exactly like the real pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.bench_media import Bot, encode, tts_wav
from tests.fixtures import lottie_text_tgs

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector
from translator_bot.languages import LANGUAGES
from translator_bot.media.processor import MediaProcessor
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import TranslationJob, TranslationProvider
from translator_bot.providers.deepl import DeepLProvider
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.storage import Storage
from translator_bot.textutil import looks_garbled, wrong_script

CHROME = next((p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                           shutil.which("chromium") or "", shutil.which("google-chrome") or "") if p and Path(p).exists()),
              None)
FONT_STACK = ("'Segoe UI','Nirmala UI','Leelawadee UI','Microsoft YaHei','Yu Gothic','Malgun Gothic',"
              "'Tahoma',sans-serif")


# --- rendering ---------------------------------------------------------------------------------------------------


def render(body: str, w: int = 512, h: int = 512, bg: str = "transparent") -> bytes:
    """Screenshot an HTML fragment with headless Chrome (transparent background unless `bg` is set)."""
    assert CHROME, "needs Chrome/Edge"
    html = (f"<!doctype html><html><head><meta charset='utf-8'><style>html,body{{margin:0;width:{w}px;height:{h}px;"
            f"overflow:hidden;background:{bg}}} .t{{position:absolute;width:{w}px;text-align:center;font-weight:900;"
            f"font-family:{FONT_STACK};color:#fff;-webkit-text-stroke:6px #000;paint-order:stroke fill;"
            f"line-height:1.15}} .e{{position:absolute;width:{w}px;text-align:center;font-family:'Segoe UI Emoji'}}"
            f"</style></head><body>{body}</body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "p.html", Path(tmp) / "p.png"
        src.write_text(html, "utf-8")
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                        "--default-background-color=00000000", f"--window-size={w},{h}", f"--screenshot={out}",
                        src.as_uri()], capture_output=True, timeout=60, check=False)
        return out.read_bytes()


def sticker_html(text: str, emoji: str = "😂", *, top: int = 18, size: int = 64, extra: str = "") -> str:
    return (f"<div class='e' style='top:150px;font-size:250px'>{emoji}</div>"
            f"<div class='t' style='top:{top}px;font-size:{size}px;{extra}'>{text}</div>")


def webp(png: bytes) -> bytes:
    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGBA").save(buf, "WEBP", quality=90)
    return buf.getvalue()


def jpeg(png: bytes) -> bytes:
    buf = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


def gif89(frames: list[bytes], ms: int = 500) -> bytes:
    ims = [Image.open(io.BytesIO(f)).convert("RGB") for f in frames]
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=ms, loop=0)
    return buf.getvalue()


def static(text: str, emoji: str = "😂", **kw: Any) -> bytes:
    return webp(render(sticker_html(text, emoji, **kw)))


def slide_in(text: str, emoji: str, n: int = 6) -> list[bytes]:
    """Text slides down and fades in, then holds — like typical animated text stickers."""
    out = []
    for i in range(n):
        k = min(1.0, i / 3)
        out.append(render(sticker_html(text, emoji, top=int(-60 + 78 * k), extra=f"opacity:{k:.2f}")))
    return out


def speech_ogg(text: str, *, degrade: bool = False) -> bytes | None:
    wav = tts_wav(text)
    if wav is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.wav").write_bytes(wav)
        out = Path(tmp, "v.ogg")
        af = (["-af", "atempo=1.15,highpass=f=300,lowpass=f=3000,volume=0.8", "-ar", "8000"] if degrade else [])
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(Path(tmp, "a.wav"))]
        if degrade:  # mix in pink noise: a phone in a noisy street
            cmd += ["-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.05", "-filter_complex",
                    "[0:a]atempo=1.15,highpass=f=300,lowpass=f=3000[s];[s][1:a]amix=inputs=2:duration=first[m]",
                    "-map", "[m]", "-ar", "8000"]
        cmd += [*([] if degrade else af), "-c:a", "libopus", "-b:a", "12k" if degrade else "24k", "-ac", "1", str(out)]
        subprocess.run(cmd, check=True)
        return out.read_bytes()


def video_speech(text: str, screen_png: bytes) -> bytes | None:
    wav = tts_wav(text)
    if wav is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.wav").write_bytes(wav)
        Path(tmp, "f.png").write_bytes(screen_png)
        out = Path(tmp) / "v.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(Path(tmp, "f.png")), "-i",
                        str(Path(tmp, "a.wav")), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-vf", "scale=480:480", str(out)], check=True)
        return out.read_bytes()


# --- cases (written before looking at any output) -----------------------------------------------------------------


def cases() -> list[dict[str, Any]]:
    c: list[dict[str, Any]] = []

    def add(id_: str, kind: str, data: bytes | None, src: str | None, text: str, meaning: str, **kw: Any) -> None:
        if data:
            c.append({"id": id_, "kind": kind, "data": data, "src": src, "expect": text, "meaning": meaning,
                      "has_text": bool(text), **kw})

    stickers = [  # (id, src, text, emoji, English meaning)
        ("st-en", "en", "BRUH 💀", "😐", "bruh (disbelief)"),
        ("st-es", "es", "NO MAMES WEY", "😱", "no way, dude (Mexican slang)"),
        ("st-pt", "pt", "KKKKK QUE ISSO", "🤣", "lol what is this"),
        ("st-fr", "fr", "MDR T'ES SÉRIEUX ?", "😂", "lol are you serious?"),
        ("st-de", "de", "ALTER, WAS?!", "😳", "dude, what?!"),
        ("st-tr", "tr", "YOK ARTIK", "😲", "no way / you're kidding"),
        ("st-id", "id", "WKWKWK GAS TERUS", "😆", "lol keep going"),
        ("st-vi", "vi", "ĐỈNH CỦA CHÓP", "🔥", "top-tier / the best"),
        ("st-tl", "tl", "GRABE KA TALAGA", "😅", "you're really something (wow, you)"),
        ("st-ru", "ru", "ЖИЗА", "😩", "so relatable (that's life)"),
        ("st-uk", "uk", "ТРИМАЙСЯ", "💪", "hang in there"),
        ("st-el", "el", "ΕΛΑ ΡΕ!", "🙄", "come on, man!"),
        ("st-ar", "ar", "يا حبيبي ليش كذا", "😂", "oh my dear, why like this"),
        ("st-fa", "fa", "دمت گرم", "👏", "well done / thanks a lot"),
        ("st-he", "he", "יאללה בלגן", "🎉", "let's go, let's party"),
        ("st-hi", "hi", "मज़ा आ गया", "😄", "that was so much fun"),
        ("st-bn", "bn", "দারুণ হয়েছে", "👍", "that turned out great"),
        ("st-ta", "ta", "சூப்பர் மச்சான்", "😎", "super, buddy"),
        ("st-th", "th", "555 ตลกมาก", "🤣", "lol so funny"),
        ("st-zh", "zh", "笑死我了", "🤣", "I'm dying of laughter"),
        ("st-ja", "ja", "草生える", "🤣", "lol (that's hilarious)"),
        ("st-ko", "ko", "ㅋㅋㅋ 미쳤다", "🤪", "lol that's crazy"),
        ("st-hinglish", "hi", "BHAI KYA SCENE HAI", "😏", "bro, what's the plan / what's up"),
    ]
    for id_, src, text, emoji, meaning in stickers:
        add(id_, "sticker", static(text, emoji), src, text, meaning)

    # hard layouts
    meme = render("<div class='t' style='top:14px;font-size:46px'>जब बॉस बोले</div>"
                  "<div class='e' style='top:150px;font-size:200px'>🤡</div>"
                  "<div class='t' style='top:420px;font-size:46px'>आज ओवरटाइम है</div>", bg="#3b6aa0")
    add("meme-hi", "image", jpeg(meme), "hi", "जब बॉस बोले आज ओवरटाइम है", "when the boss says: overtime today")
    busy = render("<div style='position:absolute;inset:0;background:repeating-linear-gradient(45deg,#e33 0 14px,"
                  "#fc3 14px 28px,#3a6 28px 42px)'></div><div class='t' style='top:400px;font-size:30px;"
                  "-webkit-text-stroke:4px #000'>صباح الخير يا جماعة</div>")
    add("busy-ar", "image", jpeg(busy), "ar", "صباح الخير يا جماعة", "good morning, everyone")
    add("vert-ja", "sticker", webp(render("<div class='e' style='top:150px;left:-90px;font-size:240px'>🐱</div>"
                                          "<div class='t' style='top:20px;left:190px;font-size:62px;"
                                          "writing-mode:vertical-rl;width:auto'>がんばって</div>")),
        "ja", "がんばって", "do your best / good luck")
    add("hand-en", "sticker", static("miss u already 🥺", "🥺",
                                    extra="font-family:'Comic Sans MS';font-weight:700;color:#ff5fa2"),
        "en", "miss u already", "miss you already")
    add("emoji-only", "sticker", webp(render("<div class='e' style='top:90px;font-size:300px'>😂🔥</div>")), None, "",
        "(no text)")
    add("watermark-only", "sticker", webp(render("<div class='e' style='top:90px;font-size:300px'>🐸</div>"
                                                 "<div style='position:absolute;right:8px;bottom:6px;font:12px "
                                                 "sans-serif;color:#999'>@funnypack_bot</div>")), None, "",
        "(watermark only: nothing to translate)")

    # animated
    add("vst-ru", "video_sticker", encode(slide_in("ПОГНАЛИ!", "🚀"), ".webm", 0.25), "ru", "ПОГНАЛИ!", "let's go!")
    add("vst-ja", "video_sticker", encode(slide_in("やったー！", "🙌"), ".webm", 0.25), "ja", "やったー！", "yay! I did it!")
    add("vst-ar", "video_sticker", encode(slide_in("يلا بينا", "🏃"), ".webm", 0.25), "ar", "يلا بينا", "let's go")
    add("gif89-es", "gif", gif89([render(sticker_html("¿EN SERIO?", "🤨"), bg="#222")] * 3), "es", "¿EN SERIO?",
        "seriously?")
    seq = [render(sticker_html(t, "🧐"), bg="#2a2a40") for t in ("잠깐만", "잠깐만", "뭐야 이거", "뭐야 이거")]
    add("gif-ko-seq", "gif", encode(seq, ".mp4", 0.6), "ko", "잠깐만 뭐야 이거", "wait... what is this")
    sub = render("<div class='e' style='top:60px;font-size:260px'>🍕</div><div class='t' style='top:430px;"
                 "font-size:34px;font-weight:700;-webkit-text-stroke:4px #000'>quando a comida chega</div>", bg="#555")
    add("gif-pt-sub", "gif", encode([sub] * 4, ".mp4", 0.5), "pt", "quando a comida chega", "when the food arrives")
    add("tgs-ru", "animated_sticker", lottie_text_tgs("ПРИВЕТ"), "ru", "ПРИВЕТ", "hi / hello")
    add("tgs-el", "animated_sticker", lottie_text_tgs("ΓΕΙΑ ΣΟΥ"), "el", "ΓΕΙΑ ΣΟΥ", "hello")

    # speech (Windows SAPI: English voices only)
    line = "Hey guys, I am stuck in traffic. Start without me, I will be there in twenty minutes."
    add("voice-en", "voice", speech_ogg(line), "en", line, "same", duration=6)
    add("voice-en-noisy", "voice", speech_ogg(line, degrade=True), "en", line, "same (noisy, 8 kHz, 1.15x)",
        duration=6)
    screen = render("<div class='t' style='top:190px;font-size:70px'>OFERTA HOY</div>", bg="#b33")
    add("video-mixed", "video", video_speech("Check this out, the store has a big sale today.", screen), "en",
        "OFERTA HOY + Check this out, the store has a big sale today.", "today's offer + spoken English",
        duration=5)
    return c


# --- scoring -----------------------------------------------------------------------------------------------------


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if unicodedata.category(ch)[0] in "LMN")


def similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, norm(a), norm(b)).ratio(), 2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="en,hi,ru,ar")
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--only", default="", help="comma-separated id prefixes")
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
    print("rendering fixtures…", flush=True)
    all_cases = cases()
    if args.only:
        pre = tuple(args.only.split(","))
        all_cases = [x for x in all_cases if x["id"].startswith(pre)]
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        st = Storage(Path(tmp) / "m.sqlite3")
        await st.open()
        metrics = Metrics()
        proc = MediaProcessor(Bot(b""), st, metrics, vision=g, max_download_bytes=8_000_000,  # type: ignore[arg-type]
                              max_image_pixels=25_000_000, max_tgs_json_bytes=4_000_000, max_video_seconds=15,
                              max_frames=24, ffmpeg_path="ffmpeg", timeout_s=60, workers=2, ocr_min_confidence=0.6,
                              cache_ttl_s=86400)
        await proc.warmup()
        for it in all_cases:
            proc.bot = Bot(it["data"])  # type: ignore[assignment]
            t = time.perf_counter()
            err = None
            try:
                r = await proc.extract(file_id="f", file_unique_id=it["id"], size=len(it["data"]), kind=it["kind"],
                                       targets=targets, duration=it.get("duration"))
            except Exception as e:
                r, err = None, repr(e)
            ms = round((time.perf_counter() - t) * 1000)
            row: dict[str, Any] = {k: it[k] for k in ("id", "kind", "src", "expect", "meaning", "has_text")}
            row.update(ms=ms, error=err, found=bool(r), via=r.via if r else None, read=r.text if r else "",
                       read_src=r.source_lang if r else None, sim=similarity(r.text, it["expect"]) if r else 0.0)
            per: dict[str, Any] = {}
            if r:
                for tgt in targets:
                    same = (r.source_lang or it["src"] or "").split("-")[0] == tgt
                    tr = r.translations.get(tgt)
                    how = "media"
                    if tr is None and not same:  # dropped by the media checks -> text translator, as the pipeline does
                        d = det.detect(r.text, tgt)
                        res = await orch.translate(TranslationJob(r.text, tgt, style="ocr"), d, purpose="ocr")
                        tr, how = (res.translation if res and res.needs_translation else ""), "text-retry"
                    tr = tr or ""
                    lang = LANGUAGES[tgt]
                    back = ""
                    if tr and tgt != "en" and dl.supports(None, "en"):
                        try:
                            back = (await dl.translate(TranslationJob(tr, "en"), quality=False, timeout=15)).translation
                        except Exception as e:
                            back = f"(back-translation failed: {e!r})"
                    per[tgt] = {"out": tr, "how": how, "same_lang": same,
                                "script_ok": not (tr and wrong_script(tr, lang.script)),
                                "garbled": bool(tr and looks_garbled(r.text, tr, tgt)), "back_en": back}
            row["targets"] = per
            rows.append(row)
            print(f"\n[{it['id']}] {it['kind']} {ms}ms found={row['found']} via={row['via']} "
                  f"src={row['read_src']}/{it['src']} sim={row['sim']}  meaning: {it['meaning']}")
            print(f"   read: {row['read']!r}" + (f"  ERROR {err}" if err else ""))
            for tgt, p in per.items():
                flag = "" if p["script_ok"] and not p["garbled"] else "  <-- " + ("SCRIPT" if not p["script_ok"]
                                                                                  else "GARBLED")
                print(f"   {tgt} [{p['how']}]: {p['out']!r}" + (f"  (back: {p['back_en']!r})" if p["back_en"] else "")
                      + flag)
            await asyncio.sleep(args.pause)
        await proc.close()
        await st.close()
    text_rows = [r for r in rows if r["has_text"]]
    none_rows = [r for r in rows if not r["has_text"]]
    outs = [p for r in rows for p in r["targets"].values() if p["out"]]
    summary = {
        "items": len(rows), "errors": sum(bool(r["error"]) for r in rows),
        "recall": round(sum(r["found"] for r in text_rows) / max(1, len(text_rows)), 3),
        "false_positive": round(sum(r["found"] for r in none_rows) / max(1, len(none_rows)), 3),
        "mean_read_similarity": round(sum(r["sim"] for r in text_rows if r["found"])
                                      / max(1, sum(r["found"] for r in text_rows)), 3),
        "source_lang_acc": round(sum((r["read_src"] or "").split("-")[0] == r["src"] for r in text_rows if r["found"])
                                 / max(1, sum(r["found"] for r in text_rows)), 3),
        "outputs": len(outs), "script_ok": sum(p["script_ok"] for p in outs),
        "garbled": sum(p["garbled"] for p in outs), "text_retries": sum(p["how"] == "text-retry" for p in outs),
        "missing_translations": sum(1 for r in text_rows if r["found"] for p in r["targets"].values()
                                    if not p["out"] and not p["same_lang"]),
        "p50_ms": sorted(r["ms"] for r in rows)[len(rows) // 2] if rows else 0,
    }
    print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))
    path = Path(__file__).parent / "results" / f"media-matrix-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {path}")
    await g.aclose()


if __name__ == "__main__":
    asyncio.run(main())
