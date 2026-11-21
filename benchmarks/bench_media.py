"""LIVE media benchmark: real ffmpeg/rlottie/RapidOCR + real Gemini vision/audio, synthetic-but-realistic media.

    python benchmarks/bench_media.py [--targets en,hi] [--pause 1]

Per item: expected has_text vs detected, recognized text, translations per target, whether the output is
in the target language/script, vision calls, and the per-stage latency breakdown.
Speech items use Windows SAPI text-to-speech (skipped on other OSes).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, features

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from translator_bot.config import load_settings
from translator_bot.detect import LanguageDetector, Verdict
from translator_bot.media.processor import MediaProcessor
from translator_bot.metrics import Metrics
from translator_bot.providers.gemini import GeminiProvider
from translator_bot.storage import Storage

FONTS = {"latin": "arialbd.ttf", "cyr": "arialbd.ttf", "deva": "Nirmala.ttc", "arab": "tahomabd.ttf",
         "ja": "YuGothB.ttc", "ko": "malgunbd.ttf"}


def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONTS[kind], size)


def sticker(text: str, kind: str = "latin", *, size: int = 512, fsize: int = 64, rotate: float = 0, noise: bool = False,
            transparent: bool = True, fmt: str = "WEBP", meme: tuple[str, str] | None = None) -> bytes:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent else (60, 110, 170, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((size * 0.2, size * 0.25, size * 0.8, size * 0.85), fill=(255, 205, 60, 255))  # a "character"
    d.ellipse((size * 0.35, size * 0.45, size * 0.42, size * 0.52), fill=(0, 0, 0, 255))
    d.ellipse((size * 0.58, size * 0.45, size * 0.65, size * 0.52), fill=(0, 0, 0, 255))
    if noise:
        import random

        r = random.Random(7)
        for _ in range(500):
            x, y = r.randrange(size), r.randrange(size)
            d.rectangle((x, y, x + 5, y + 5), fill=(r.randrange(256), r.randrange(256), r.randrange(256), 255))
    f = font(kind, fsize)
    lines = [(meme[0], 0.04), (meme[1], 0.80)] if meme else [(text, 0.05)]
    for line, ypos in lines:
        if not line:
            continue
        kwargs: dict[str, Any] = {}
        if kind == "arab" and features.check("raqm"):
            kwargs = {"direction": "rtl", "language": "ar"}
        bbox = d.textbbox((0, 0), line, font=f, **kwargs)
        x = (size - (bbox[2] - bbox[0])) // 2
        d.text((x, int(size * ypos)), line, font=f, fill=(255, 255, 255, 255), stroke_width=4,
               stroke_fill=(0, 0, 0, 255), **kwargs)
    if rotate:
        img = img.rotate(rotate, fillcolor=(0, 0, 0, 0) if transparent else (60, 110, 170, 255))
    if size < 512:
        pass
    buf = io.BytesIO()
    (img if fmt in ("WEBP", "PNG") else img.convert("RGB")).save(buf, format=fmt)
    return buf.getvalue()


def png(text: str, kind: str = "latin") -> bytes:
    return sticker(text, kind, transparent=False, fmt="PNG")


def encode(frames: list[bytes], suffix: str, spf: float = 0.8) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        for i, fr in enumerate(frames):
            Path(tmp, f"in_{i:03d}.png").write_bytes(fr)
        out = Path(tmp) / f"o{suffix}"
        codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if suffix == ".mp4" else \
            ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "40"]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", f"{1 / spf}", "-i",
                        str(Path(tmp) / "in_%03d.png"), "-r", "10", *codec, str(out)], check=True)
        return out.read_bytes()


def tts_wav(text: str) -> bytes | None:
    if sys.platform != "win32":
        return None
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "s.wav"
        ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{text}'); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
        return wav.read_bytes()


def video_with_speech(text: str, frame_text: str | None) -> bytes | None:
    wav = tts_wav(text)
    if wav is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.wav").write_bytes(wav)
        Path(tmp, "f.png").write_bytes(png(frame_text) if frame_text else png(""))
        out = Path(tmp) / "v.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(Path(tmp, "f.png")), "-i",
                        str(Path(tmp, "a.wav")), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-vf", "scale=480:480", str(out)], check=True)
        return out.read_bytes()


def voice_ogg(text: str) -> bytes | None:
    wav = tts_wav(text)
    if wav is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.wav").write_bytes(wav)
        out = Path(tmp, "v.ogg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(Path(tmp, "a.wav")), "-c:a", "libopus",
                        "-b:a", "24k", "-ac", "1", "-ar", "16000", str(out)], check=True)
        return out.read_bytes()


def tgs_with_text(text: str, frames: int = 60) -> bytes:
    """Lottie with an embedded image layer carrying text that fades/slides in (rlottie renders image layers)."""
    img = sticker(text, transparent=True, fmt="PNG")
    asset = {"id": "t", "w": 512, "h": 512, "u": "", "p": "data:image/png;base64," + base64.b64encode(img).decode(),
             "e": 1}
    layer = {"ddd": 0, "ind": 1, "ty": 2, "nm": "txt", "refId": "t", "sr": 1, "ip": 0, "op": frames, "st": 0, "bm": 0,
             "ao": 0, "ks": {"o": {"a": 1, "k": [{"t": 0, "s": [0]}, {"t": 15, "s": [100]}]},
                             "r": {"a": 0, "k": 0}, "p": {"a": 1, "k": [{"t": 0, "s": [256, 300, 0]},
                                                                        {"t": 20, "s": [256, 256, 0]}]},
                             "a": {"a": 0, "k": [256, 256, 0]}, "s": {"a": 0, "k": [100, 100, 100]}}}
    doc = {"v": "5.5.2", "fr": 30, "ip": 0, "op": frames, "w": 512, "h": 512, "nm": "t", "ddd": 0, "assets": [asset],
           "layers": [layer]}
    return gzip.compress(json.dumps(doc).encode())


def smiley() -> bytes:
    from tests.fixtures import blank_image

    return blank_image()


def vector_tgs(text: str) -> bytes:
    from tests.fixtures import lottie_text_tgs

    return lottie_text_tgs(text)


def tgs_textless(frames: int = 60) -> bytes:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.fixtures import lottie_tgs

    return lottie_tgs(frames=frames)


def items() -> list[dict[str, Any]]:
    flash = [png("")] * 4 + [png("BOOM!")] + [png("")] * 4
    out: list[dict[str, Any]] = [
        {"id": "st-en", "kind": "sticker", "data": sticker("NO WAY BRO"), "text": True},
        {"id": "st-es", "kind": "sticker", "data": sticker("¡QUÉ ASCO!"), "text": True},
        {"id": "st-ru", "kind": "sticker", "data": sticker("НУ И НУ", "cyr"), "text": True},
        {"id": "st-hi", "kind": "sticker", "data": sticker("क्या बात है!", "deva"), "text": True},
        {"id": "st-ar", "kind": "sticker", "data": sticker("يا سلام", "arab"), "text": True,
         "note": "" if features.check("raqm") else "Pillow has no raqm: Arabic glyphs unshaped (realism reduced)"},
        {"id": "st-ja", "kind": "sticker", "data": sticker("マジかよ", "ja"), "text": True},
        {"id": "st-ko", "kind": "sticker", "data": sticker("대박", "ko", fsize=90), "text": True},
        {"id": "st-none", "kind": "sticker", "data": sticker(""), "text": False},
        {"id": "st-smiley", "kind": "sticker", "data": smiley(), "text": False},
        {"id": "st-cjk1", "kind": "sticker", "data": sticker("草", "ja", fsize=200), "text": True},
        {"id": "st-ok", "kind": "sticker", "data": sticker("OK", fsize=140), "text": True},
        {"id": "st-lowres", "kind": "sticker", "data": sticker("OMG", size=128, fsize=28), "text": True},
        {"id": "st-rot", "kind": "sticker", "data": sticker("NOPE", rotate=14), "text": True},
        {"id": "img-meme", "kind": "image", "data": sticker("", transparent=False, fmt="JPEG", noise=True,
                                                            meme=("CUANDO EL PROFE DICE", "QUE HAY EXAMEN")), "text": True},
        {"id": "gif-seq", "kind": "gif", "data": encode([png(t) for t in ("WAIT", "WAIT", "WHAT", "IS THIS")], ".mp4"),
         "text": True},
        {"id": "gif-none", "kind": "gif", "data": encode([png("")] * 5, ".mp4"), "text": False},
        {"id": "gif-flash", "kind": "gif", "data": encode(flash, ".mp4", 0.3), "text": True},
        {"id": "vst-es", "kind": "video_sticker", "data": encode([sticker("¡VAMOS!", fmt="PNG")] * 3, ".webm"),
         "text": True},
        {"id": "tgs-text-en", "kind": "animated_sticker", "data": vector_tgs("LET'S GO"), "text": True},
        {"id": "tgs-text-es", "kind": "animated_sticker", "data": vector_tgs("¡VAMOS YA!"), "text": True},
        {"id": "tgs-none", "kind": "animated_sticker", "data": tgs_textless(), "text": False},
    ]
    v = video_with_speech("Hello everyone. The meeting is moved to five o clock tomorrow.", "MEETING UPDATE")
    if v:
        out.append({"id": "video-speech", "kind": "video", "data": v, "text": True, "duration": 6})
    vo = voice_ogg("I will be a little late tonight, please start without me.")
    if vo:
        out.append({"id": "voice-en", "kind": "voice", "data": vo, "text": True, "duration": 4})
    return out


class Bot:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def get_file(self, file_id: str) -> Any:
        class F:
            file_size = len(self.data)
            file_path = "x"
        return F()

    async def download_file(self, path: str, destination: io.BytesIO, timeout: int = 30) -> None:
        destination.write(self.data)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="en,hi")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()
    targets = args.targets.split(",")
    s = load_settings()
    assert s.gemini_api_key, "needs GEMINI_API_KEY"
    g = GeminiProvider(s.gemini_api_key.get_secret_value(), fast_model=s.gemini_fast_model,
                       quality_model=s.gemini_quality_model, vision_model=s.gemini_vision_model,
                       fallback_model=s.gemini_fallback_model, rpm=s.gemini_rpm)
    det = LanguageDetector()
    with tempfile.TemporaryDirectory() as tmp:
        st = Storage(Path(tmp) / "m.sqlite3")
        await st.open()
        metrics = Metrics()
        proc = MediaProcessor(Bot(b""), st, metrics, vision=g, max_download_bytes=8_000_000,  # type: ignore[arg-type]
                              max_image_pixels=25_000_000, max_tgs_json_bytes=4_000_000, max_video_seconds=15,
                              max_frames=24, ffmpeg_path="ffmpeg", timeout_s=60, workers=2, ocr_min_confidence=0.6,
                              cache_ttl_s=86400)
        t0 = time.perf_counter()
        await proc.warmup()
        warm = round((time.perf_counter() - t0) * 1000)
        rows = []
        for it in items():
            proc.bot = Bot(it["data"])  # type: ignore[assignment]
            before = metrics.counters.get("media.vision", 0)
            t = time.perf_counter()
            r = await proc.extract(file_id="f", file_unique_id=it["id"], size=len(it["data"]), kind=it["kind"],
                                   targets=targets, duration=it.get("duration"))
            ms = round((time.perf_counter() - t) * 1000)
            row: dict[str, Any] = {"id": it["id"], "kind": it["kind"], "expect_text": it["text"],
                                   "got_text": bool(r), "text": r.text if r else "", "src": r.source_lang if r else None,
                                   "translations": r.translations if r else {}, "ms": ms,
                                   "vision_calls": metrics.counters.get("media.vision", 0) - before,
                                   "note": it.get("note", "")}
            row["correct_detection"] = row["expect_text"] == row["got_text"]
            if r:
                row["target_lang_ok"] = {t: (det.detect(tr, t).verdict is not Verdict.OTHER) for t, tr in
                                         r.translations.items() if tr}
            rows.append(row)
            print(f"{it['id']:13} {it['kind']:16} {ms:6}ms text={row['got_text']!s:5} "
                  f"{'OK ' if row['correct_detection'] else 'BAD'} {row['text'][:40]!r} -> "
                  f"{json.dumps(row['translations'], ensure_ascii=False)[:110]}")
            await asyncio.sleep(args.pause)
        await proc.close()
        await st.close()
    snap = metrics.snapshot()
    with_text = [r for r in rows if r["expect_text"]]
    no_text = [r for r in rows if not r["expect_text"]]
    summary = {
        "items": len(rows), "warmup_ms": warm,
        "text_recall": round(sum(r["got_text"] for r in with_text) / max(1, len(with_text)), 3),
        "textless_false_positive": round(sum(r["got_text"] for r in no_text) / max(1, len(no_text)), 3),
        "vision_calls_on_textless": sum(r["vision_calls"] for r in no_text),
        "target_lang_ok": round(sum(v for r in rows for v in r.get("target_lang_ok", {}).values())
                                / max(1, sum(len(r.get("target_lang_ok", {})) for r in rows)), 3),
        "ms_by_kind": {k: sorted(r["ms"] for r in rows if r["kind"] == k) for k in sorted({r["kind"] for r in rows})},
        "stages_ms": {k: v for k, v in snap["latency_ms"].items() if k.startswith("media_stage")},  # type: ignore[union-attr]
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    path = Path(__file__).parent / "results" / f"media-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {path}")
    await g.aclose()


if __name__ == "__main__":
    asyncio.run(main())
