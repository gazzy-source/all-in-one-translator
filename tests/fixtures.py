"""Synthetic media fixtures generated on the fly (no binary files in the repo)."""

from __future__ import annotations

import gzip
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def text_image(text: str, *, size: tuple[int, int] = (512, 512), fmt: str = "PNG", transparent: bool = False,
               fg: tuple[int, int, int] = (255, 255, 255), outline: bool = True, rotate: float = 0,
               noise: bool = False, font_size: int = 72) -> bytes:
    mode = "RGBA" if transparent else "RGB"
    bg = (0, 0, 0, 0) if transparent else (40, 90, 160)
    img = Image.new(mode, size, bg)
    d = ImageDraw.Draw(img)
    if noise:
        import random

        rnd = random.Random(42)
        for _ in range(400):
            x, y = rnd.randrange(size[0]), rnd.randrange(size[1])
            d.ellipse((x, y, x + 6, y + 6), fill=(rnd.randrange(255), rnd.randrange(255), rnd.randrange(255)))
    f = _font(font_size)
    bbox = d.multiline_textbbox((0, 0), text, font=f, align="center")
    # Fonts differ by OS (Arial Bold on Windows, wider DejaVu Sans Bold on Linux CI): shrink until the text fits,
    # otherwise both ends are clipped ("HELLO FRIEND" -> "JELLO FRIEN").
    while font_size > 12 and (bbox[2] - bbox[0] > size[0] * 0.92 or bbox[3] - bbox[1] > size[1] * 0.92):
        font_size = int(font_size * 0.9)
        f = _font(font_size)
        bbox = d.multiline_textbbox((0, 0), text, font=f, align="center")
    x = (size[0] - (bbox[2] - bbox[0])) // 2
    y = (size[1] - (bbox[3] - bbox[1])) // 2
    d.multiline_text((x, y), text, font=f, fill=fg, align="center",
                     stroke_width=4 if outline else 0, stroke_fill=(0, 0, 0))
    if rotate:
        img = img.rotate(rotate, expand=False, fillcolor=bg)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def blank_image(fmt: str = "WEBP", transparent: bool = True) -> bytes:
    img = Image.new("RGBA" if transparent else "RGB", (512, 512), (0, 0, 0, 0) if transparent else (200, 50, 50))
    d = ImageDraw.Draw(img)
    d.ellipse((100, 100, 412, 412), fill=(250, 200, 0, 255))  # a "smiley" with no text
    d.ellipse((190, 200, 230, 240), fill=(0, 0, 0, 255))
    d.ellipse((282, 200, 322, 240), fill=(0, 0, 0, 255))
    d.arc((170, 220, 342, 360), 20, 160, fill=(0, 0, 0, 255), width=10)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def video_from_frames(frames: list[bytes], *, suffix: str = ".mp4", seconds_per_frame: float = 0.8) -> bytes:
    """Encode frames with ffmpeg (mp4/h264 for GIF-like animations, webm/vp9 for video stickers)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not available")
    with tempfile.TemporaryDirectory() as tmp:
        for i, fr in enumerate(frames):
            Path(tmp, f"in_{i:03d}.png").write_bytes(fr)
        out = Path(tmp) / f"out{suffix}"
        codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"] if suffix == ".mp4" else \
            ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "40"]
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-framerate", f"{1 / seconds_per_frame}",
                        "-i", str(Path(tmp) / "in_%03d.png"), "-r", "10", *codec,
                        "-vf", "scale=512:512", str(out)], check=True, capture_output=True)
        return out.read_bytes()


def lottie_tgs(*, frames: int = 60, text_like: bool = False) -> bytes:
    """Minimal valid Lottie doc (a moving circle; optionally thick bars that look like text strokes)."""
    shapes = [{
        "ty": "gr", "it": [
            {"ty": "el", "p": {"a": 0, "k": [256, 256]}, "s": {"a": 0, "k": [200, 200]}},
            {"ty": "fl", "c": {"a": 0, "k": [1, 0.8, 0, 1]}, "o": {"a": 0, "k": 100}},
            {"ty": "tr", "p": {"a": 1, "k": [{"t": 0, "s": [0, 0]}, {"t": frames, "s": [60, 0]}]},
             "a": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0},
             "o": {"a": 0, "k": 100}},
        ],
    }]
    doc = {"v": "5.5.2", "fr": 30, "ip": 0, "op": frames, "w": 512, "h": 512, "nm": "t", "ddd": 0, "assets": [],
           "layers": [{"ddd": 0, "ind": 1, "ty": 4, "nm": "c", "sr": 1, "ks": {
               "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0}, "p": {"a": 0, "k": [0, 0, 0]},
               "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
               "ao": 0, "shapes": shapes, "ip": 0, "op": frames, "st": 0, "bm": 0}]}
    return gzip.compress(json.dumps(doc).encode())


def gzip_bomb(json_bytes: int = 50 * 1024 * 1024) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        chunk = b" " * (1024 * 1024)
        for _ in range(json_bytes // len(chunk)):
            gz.write(chunk)
    return buf.getvalue()


def lottie_text_tgs(text: str, *, font_path: str = "C:/Windows/Fonts/arialbd.ttf", frames: int = 60,
                    size: int = 512) -> bytes:
    """Realistic animated text sticker: glyph outlines converted to Lottie vector shapes (Telegram TGS forbids
    image layers, so real text stickers draw text as shapes). The text slides in and fades in."""
    from fontTools.pens.recordingPen import RecordingPen
    from fontTools.ttLib import TTFont

    ft = TTFont(font_path, fontNumber=0)
    cmap = ft.getBestCmap()
    gs = ft.getGlyphSet()
    upm = ft["head"].unitsPerEm
    scale = (size * 0.8) / (upm * max(1, len(text)) * 0.6)
    scale = min(scale, size * 0.25 / upm * 1.6)
    x = 0.0
    shapes: list[dict[str, object]] = []
    total_w = sum(gs[cmap.get(ord(ch), ".notdef")].width for ch in text) * scale
    x0 = (size - total_w) / 2
    baseline = size * 0.62
    for ch in text:
        gname = cmap.get(ord(ch), ".notdef")
        pen = RecordingPen()
        gs[gname].draw(pen)
        contour: list[tuple[float, float, tuple[float, float], tuple[float, float]]] = []

        def P(pt: tuple[float, float]) -> tuple[float, float]:
            return (x0 + x + pt[0] * scale, baseline - pt[1] * scale)

        cur: tuple[float, float] | None = None
        verts: list[list[float]] = []
        ins: list[list[float]] = []
        outs: list[list[float]] = []

        def flush() -> None:
            if verts:
                shapes.append({"ty": "sh", "ks": {"a": 0, "k": {"c": True, "v": [v[:] for v in verts],
                                                                 "i": [i[:] for i in ins], "o": [o[:] for o in outs]}}})
            verts.clear(); ins.clear(); outs.clear()

        for op, args in pen.value:
            if op == "moveTo":
                flush()
                cur = P(args[0])
                verts.append(list(cur)); ins.append([0, 0]); outs.append([0, 0])
            elif op == "lineTo" and cur is not None:
                cur = P(args[0])
                verts.append(list(cur)); ins.append([0, 0]); outs.append([0, 0])
            elif op == "qCurveTo" and cur is not None:
                pts = [P(p) for p in args]
                # TrueType implied on-curve points between consecutive off-curve points
                offs, end = pts[:-1], pts[-1]
                seq: list[tuple[tuple[float, float], tuple[float, float]]] = []
                for k, off in enumerate(offs):
                    nxt = end if k == len(offs) - 1 else ((off[0] + offs[k + 1][0]) / 2, (off[1] + offs[k + 1][1]) / 2)
                    seq.append((off, nxt))
                for off, on in seq:
                    c1 = (cur[0] + 2 / 3 * (off[0] - cur[0]), cur[1] + 2 / 3 * (off[1] - cur[1]))
                    c2 = (on[0] + 2 / 3 * (off[0] - on[0]), on[1] + 2 / 3 * (off[1] - on[1]))
                    outs[-1] = [c1[0] - cur[0], c1[1] - cur[1]]
                    verts.append([on[0], on[1]]); ins.append([c2[0] - on[0], c2[1] - on[1]]); outs.append([0, 0])
                    cur = on
            elif op == "curveTo" and cur is not None:
                c1, c2, on = (P(p) for p in args)
                outs[-1] = [c1[0] - cur[0], c1[1] - cur[1]]
                verts.append([on[0], on[1]]); ins.append([c2[0] - on[0], c2[1] - on[1]]); outs.append([0, 0])
                cur = on
            elif op in ("closePath", "endPath"):
                flush()
                cur = None
        flush()
        del contour
        x += gs[gname].width * scale
    group = {"ty": "gr", "it": [*shapes,
                                {"ty": "st", "c": {"a": 0, "k": [0, 0, 0, 1]}, "o": {"a": 0, "k": 100},
                                 "w": {"a": 0, "k": 10}, "lc": 2, "lj": 2},
                                {"ty": "fl", "c": {"a": 0, "k": [1, 1, 1, 1]}, "o": {"a": 0, "k": 100}, "r": 1},
                                {"ty": "tr", "p": {"a": 0, "k": [0, 0]}, "a": {"a": 0, "k": [0, 0]},
                                 "s": {"a": 0, "k": [100, 100]}, "r": {"a": 0, "k": 0}, "o": {"a": 0, "k": 100}}]}
    layer = {"ddd": 0, "ind": 1, "ty": 4, "nm": "text", "sr": 1, "ip": 0, "op": frames, "st": 0, "bm": 0, "ao": 0,
             "ks": {"o": {"a": 1, "k": [{"t": 0, "s": [0], "i": {"x": [0.5], "y": [1]}, "o": {"x": [0.5], "y": [0]}},
                                        {"t": 12, "s": [100]}]},
                    "r": {"a": 0, "k": 0},
                    "p": {"a": 1, "k": [{"t": 0, "s": [0, 60, 0], "i": {"x": 0.5, "y": 1}, "o": {"x": 0.5, "y": 0}},
                                        {"t": 18, "s": [0, 0, 0]}]},
                    "a": {"a": 0, "k": [0, 0, 0]}, "s": {"a": 0, "k": [100, 100, 100]}},
             "shapes": [group]}
    doc = {"v": "5.5.2", "fr": 30, "ip": 0, "op": frames, "w": size, "h": size, "nm": "t", "ddd": 0, "assets": [],
           "layers": [layer]}
    return gzip.compress(json.dumps(doc).encode())
