"""Smoke test run INSIDE the production image (CI): native media dependencies work on a read-only root FS.

    docker run --rm --read-only --tmpfs /tmp -v "$PWD/scripts:/smoke:ro" IMAGE python /smoke/container_smoke.py

Self-contained (no test fixtures, no network, no secrets). Exits non-zero on the first failure.
"""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def check(name: str, ok: bool) -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {name}", flush=True)
    if not ok:
        sys.exit(1)


def text_png(text: str) -> bytes:
    img = Image.new("RGB", (512, 256), (40, 90, 160))
    d = ImageDraw.Draw(img)
    d.text((30, 80), text, fill=(255, 255, 255), font=ImageFont.load_default(size=72))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main() -> None:
    import translator_bot
    from translator_bot.media import frames as F

    print("translator_bot", translator_bot.__version__)
    check("ffmpeg present", subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0)

    # real GIF89a through the forced gif demuxer
    ims = [Image.open(io.BytesIO(text_png(t))) for t in ("WAIT", "WHAT")]
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=500, loop=0)
    fs = F.extract_video_frames(buf.getvalue(), ffmpeg="ffmpeg", suffix=F.sniff_container(buf.getvalue(), ".mp4"),
                                max_seconds=5, max_frames=4, timeout=30)
    check("GIF decode (ffmpeg, forced demuxer)", len(fs.frames) >= 2)

    # MP4 (H.264) and Ogg/Opus audio produced by ffmpeg itself
    with tempfile.TemporaryDirectory() as tmp:
        mp4, ogg = Path(tmp) / "v.mp4", Path(tmp) / "a.ogg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=160x120:d=1", "-f", "lavfi",
                        "-i", "sine=f=440:d=1", "-shortest", "-pix_fmt", "yuv420p", str(mp4)], check=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:a", "libopus",
                        str(ogg)], check=True)
        check("MP4 frames", len(F.extract_video_frames(mp4.read_bytes(), ffmpeg="ffmpeg", suffix=".mp4",
                                                       max_seconds=5, max_frames=4, timeout=30).frames) >= 1)
        check("audio extract (Opus)", bool(F.extract_audio(ogg.read_bytes(), ffmpeg="ffmpeg", suffix=".ogg",
                                                         max_seconds=5, timeout=30)))

    # TGS (Lottie) via rlottie
    doc = {"v": "5.5.2", "fr": 30, "ip": 0, "op": 30, "w": 256, "h": 256, "nm": "s", "ddd": 0, "assets": [],
           "layers": [{"ddd": 0, "ind": 1, "ty": 4, "nm": "c", "sr": 1, "ip": 0, "op": 30, "st": 0, "bm": 0,
                       "ks": {"o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0}, "p": {"a": 0, "k": [128, 128, 0]},
                              "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
                       "shapes": [{"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [120, 120]}},
                                  {"ty": "fl", "c": {"a": 0, "k": [1, 0.8, 0, 1]}, "o": {"a": 0, "k": 100}}]}]}
    tgs = F.render_tgs(gzip.compress(json.dumps(doc).encode()), 4_000_000, 4)
    check("TGS render (rlottie)", len(tgs.frames) >= 1)

    # local OCR gate (RapidOCR / onnxruntime / OpenCV)
    png = text_png("HELLO WORLD")
    found = F.detect_text([png], [0])
    check("text detector (RapidOCR)", bool(found) and found[0].boxes > 0)
    check("blank image has no text", F.detect_text([F.load_image(text_png(""), 25_000_000).frames[0]], [0])[0].boxes == 0)

    # language gate loads its data (wordfreq / lingua) without writing to the read-only FS
    from translator_bot.detect import LanguageDetector

    det = LanguageDetector()
    check("language gate", det.detect("¿Alguien sabe si mañana estará abierto?", "en").lang == "es")
    print("container smoke: all OK")


if __name__ == "__main__":
    main()
