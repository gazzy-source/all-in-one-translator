"""CPU-bound media helpers. Every function here is top-level and picklable: they run in a process pool.

Safety: Pillow decompression-bomb limits, gzip size limits for TGS, frame/dimension caps, no shell.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

GRAY_BG = (127, 127, 127, 255)  # neutral background: white and black text both stay visible
VISION_MAX_SIDE = 768
SQUARE_BOX_MIN_REC = 0.2
OCR_MAX_SIDE = 640  # text-gate resolution: detection cost scales with pixels (~0.2 s at 640 px)


class MediaRejected(Exception):
    pass


@dataclass(slots=True)
class FrameSet:
    frames: list[bytes]  # PNG bytes, RGB, background-composited, time-ordered
    width: int
    height: int
    animated: bool
    source_frames: int


# --- image ---------------------------------------------------------------------------------------


def _flatten(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, GRAY_BG)
        bg.alpha_composite(rgba)
        return bg.convert("RGB")
    return img.convert("RGB")


def _fit(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    elif max(w, h) < 256:  # tiny stickers/emoji: upscale for OCR legibility
        f = 512 / max(w, h)
        img = img.resize((int(w * f), int(h * f)), Image.Resampling.LANCZOS)
    return img


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def load_image(data: bytes, max_pixels: int) -> FrameSet:
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ("WEBP", "PNG", "JPEG", "GIF"):
                raise MediaRejected(f"format {probe.format}")
            w, h = probe.size
            if w * h > max_pixels or w > 8000 or h > 8000:
                raise MediaRejected("dimensions")
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)
            oriented: Image.Image = ImageOps.exif_transpose(im) or im
            img = _fit(_flatten(oriented), VISION_MAX_SIDE)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaRejected("decompression bomb") from exc
    except MediaRejected:
        raise
    except Exception as exc:
        raise MediaRejected(f"invalid image: {type(exc).__name__}") from exc
    return FrameSet([_png(img)], img.width, img.height, False, 1)


# --- TGS (Lottie) ---------------------------------------------------------------------------------


def _read_tgs_json(data: bytes, max_json_bytes: int) -> str:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            raw = gz.read(max_json_bytes + 1)
    except (OSError, EOFError) as exc:
        raise MediaRejected("not gzip") from exc
    if len(raw) > max_json_bytes:
        raise MediaRejected("tgs too large (gzip bomb guard)")
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MediaRejected("invalid lottie json") from exc
    if not isinstance(doc, dict) or not {"w", "h", "ip", "op", "fr", "layers"} <= doc.keys():
        raise MediaRejected("not a lottie document")
    w, h, fr = int(doc["w"]), int(doc["h"]), float(doc["fr"])
    frames = float(doc["op"]) - float(doc["ip"])
    if not (0 < w <= 1024 and 0 < h <= 1024 and 0 < fr <= 120 and 0 < frames <= 1800):
        raise MediaRejected("lottie bounds")
    if _has_external_refs(doc):
        raise MediaRejected("lottie external asset")
    return raw.decode("utf-8")


def _has_external_refs(doc: dict[str, Any]) -> bool:
    """Image layers/assets (and font files) make rlottie open paths on *this* machine: a crafted sticker could
    pull local files into the frames we send to the vision model. Telegram's TGS format forbids them anyway."""
    fonts = doc.get("fonts")
    if isinstance(fonts, dict) and any(isinstance(f, dict) and f.get("fPath") for f in fonts.get("list") or []):
        return True
    layer_lists = [doc.get("layers")]
    for asset in doc.get("assets") or []:
        if not isinstance(asset, dict):
            return True
        if "p" in asset or "u" in asset:  # image asset (file name / directory)
            return True
        layer_lists.append(asset.get("layers"))
    for layers in layer_lists:
        if layers is None:
            continue
        if not isinstance(layers, list):
            return True
        if any(not isinstance(layer, dict) or layer.get("ty") == 2 for layer in layers):  # ty 2 = image layer
            return True
    return False


def render_tgs(data: bytes, max_json_bytes: int, max_frames: int, size: int = 384) -> FrameSet:
    from rlottie_python import LottieAnimation  # type: ignore[attr-defined]  # optional dependency

    doc = _read_tgs_json(data, max_json_bytes)
    anim = LottieAnimation.from_data(doc)
    try:
        total = int(anim.lottie_animation_get_totalframe())
        n = max(1, min(max_frames, total))
        positions = sorted({round(i * (total - 1) / max(1, n - 1)) for i in range(n)}) if total > 1 else [0]
        out = []
        for pos in positions:
            frame = anim.render_pillow_frame(frame_num=pos, width=size, height=size)
            out.append(_png(_flatten(frame)))
    finally:
        del anim
    return FrameSet(out, size, size, True, total)


# --- video (WEBM sticker, GIF/MP4 animation) -----------------------------------------------------


# Demuxer per expected container. ffmpeg picks a demuxer by probing the *content*, so a "video" that is really
# an HLS playlist or concat list could read local files (file:// is allowed for our temp input). Forcing the
# demuxer and whitelisting only it means nothing else is ever parsed.
_DEMUXER = {".mp4": "mov", ".mov": "mov", ".m4a": "mov", ".webm": "matroska", ".gif": "gif", ".ogg": "ogg",
            ".oga": "ogg", ".mp3": "mp3"}


def _input_args(suffix: str) -> list[str]:
    fmt = _DEMUXER[suffix]
    args = ["-protocol_whitelist", "file", "-format_whitelist", fmt, "-f", fmt]
    if fmt == "mov":
        args += ["-enable_drefs", "0"]  # never follow MP4 external data references (off by default; be explicit)
    return args


def extract_video_frames(data: bytes, *, ffmpeg: str, suffix: str, max_seconds: float, max_frames: int,
                         timeout: float, fps: float = 4.0, width: int = 384) -> FrameSet:
    if suffix not in (".webm", ".mp4", ".gif"):
        raise MediaRejected("suffix")
    with tempfile.TemporaryDirectory(prefix="bb_media_") as tmp:
        src = Path(tmp) / f"in{suffix}"  # fixed safe names: no user-controlled paths
        src.write_bytes(data)
        out_pattern = str(Path(tmp) / "f_%03d.png")
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
               *_input_args(suffix), "-t", f"{max_seconds:.2f}"]
        if suffix == ".webm":
            cmd += ["-c:v", "libvpx-vp9"]  # decoder with alpha support for video stickers
        cmd += ["-i", str(src), "-an", "-sn",
                "-vf", f"fps={fps},scale='min({width},iw)':-2:flags=lanczos,format=rgba",
                "-frames:v", str(max_frames), out_pattern]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False,  # noqa: S603  # nosec B603
                                  env={"PATH": os.environ.get("PATH", "")})
        except subprocess.TimeoutExpired as exc:
            raise MediaRejected("ffmpeg timeout") from exc
        except FileNotFoundError as exc:
            raise MediaRejected("ffmpeg not installed") from exc
        files = sorted(Path(tmp).glob("f_*.png"))
        if proc.returncode != 0 and not files:
            if suffix == ".webm":  # ffmpeg build without libvpx: retry with the native VP9 decoder (no alpha)
                return _native_webm(data, ffmpeg, max_seconds, max_frames, timeout, fps, width)
            raise MediaRejected("ffmpeg failed")
        frames = []
        w = h = 0
        for f in files[:max_frames]:
            with Image.open(f) as im:
                img = _flatten(im)
                w, h = img.size
                frames.append(_png(img))
    if not frames:
        raise MediaRejected("no frames")
    return FrameSet(frames, w, h, len(frames) > 1, len(frames))


def _native_webm(data: bytes, ffmpeg: str, max_seconds: float, max_frames: int, timeout: float, fps: float,
                 width: int) -> FrameSet:
    with tempfile.TemporaryDirectory(prefix="bb_media_") as tmp:
        src = Path(tmp) / "in.webm"
        src.write_bytes(data)
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", *_input_args(".webm"),
               "-t", f"{max_seconds:.2f}", "-i", str(src), "-an", "-vf",
               f"fps={fps},scale='min({width},iw)':-2", "-frames:v", str(max_frames), str(Path(tmp) / "f_%03d.png")]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout, check=False,  # noqa: S603  # nosec B603
                           env={"PATH": os.environ.get("PATH", "")})
        except subprocess.TimeoutExpired as exc:
            raise MediaRejected("ffmpeg timeout") from exc
        frames = []
        w = h = 0
        for f in sorted(Path(tmp).glob("f_*.png"))[:max_frames]:
            with Image.open(f) as im:
                img = _flatten(im)
                w, h = img.size
                frames.append(_png(img))
    if not frames:
        raise MediaRejected("no frames")
    return FrameSet(frames, w, h, len(frames) > 1, len(frames))


# --- frame selection ------------------------------------------------------------------------------


def dhash(png: bytes, size: int = 8) -> int:
    with Image.open(io.BytesIO(png)) as im:
        g = im.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
        px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | int(px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return bits


def _thumb(png: bytes, size: int = 64):  # type: ignore[no-untyped-def]
    import numpy as np

    with Image.open(io.BytesIO(png)) as im:
        return np.asarray(im.convert("L").resize((size, size), Image.Resampling.BILINEAR), dtype=np.int16)


def select_distinct(frames: list[bytes], *, changed_frac: float = 0.012, pixel_delta: int = 40,
                    limit: int = 12) -> list[int]:
    """Indices of visually distinct frames, always keeping the first.

    A frame is kept when >= `changed_frac` of 64x64 grayscale pixels differ by > `pixel_delta` from the
    last KEPT frame (so slow progressive text reveals still accumulate into a change).
    Adaptive: static scenes collapse to 1 frame; frames where content (e.g. text) changes survive.
    (A global dHash misses text changes on a static background.)
    """
    if not frames:
        return []
    keep = [0]
    last = _thumb(frames[0])
    for i in range(1, len(frames)):
        cur = _thumb(frames[i])
        if float((abs(cur - last) > pixel_delta).mean()) >= changed_frac:
            keep.append(i)
            last = cur
    if len(keep) > limit:  # evenly subsample, keep order
        step = len(keep) / limit
        keep = [keep[int(i * step)] for i in range(limit)]
    return keep


# --- local OCR (RapidOCR) -------------------------------------------------------------------------

_ENGINE: Any = None


def _engine() -> Any:
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR  # optional dependency

        _ENGINE = RapidOCR()
    return _ENGINE


@dataclass(slots=True)
class OcrFrame:
    index: int
    text_area: float  # fraction of the frame covered by detected text boxes
    boxes: int
    text: str  # recognized text (RapidOCR models: Latin + CJK; other scripts not recognized)
    score: float  # mean recognition confidence


def _poly_area(box: list[list[float]]) -> float:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return max(0.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))


def ocr_frames(frames: list[bytes], indices: list[int], *, min_box_frac: float = 0.002,
               max_side: int = OCR_MAX_SIDE) -> list[OcrFrame]:
    import numpy as np

    eng = _engine()
    out: list[OcrFrame] = []
    for i in indices:
        with Image.open(io.BytesIO(frames[i])) as im:
            img = _fit(im.convert("RGB"), max_side)
            arr = np.asarray(img)
            area = float(img.width * img.height)
        result, _ = eng(arr, use_cls=False)
        boxes = []
        for box, text, score in result or []:
            frac = _poly_area(box) / area
            if frac < min_box_frac:  # tiny boxes: watermarks, pack credits, noise
                continue
            boxes.append((frac, str(text), float(score)))
        out.append(OcrFrame(
            index=i, text_area=sum(b[0] for b in boxes), boxes=len(boxes),
            text=" ".join(b[1] for b in boxes).strip(),
            score=(sum(b[2] for b in boxes) / len(boxes)) if boxes else 0.0,
        ))
    return out


def init_worker() -> None:
    """Process-pool initializer: load the OCR models once per worker, before the first media message."""
    with contextlib.suppress(Exception):  # optional dependency missing: OCR gate is skipped later
        _engine()


def warmup() -> bool:
    import numpy as np

    try:
        _engine()(np.full((64, 64, 3), 127, dtype=np.uint8), use_cls=False)
        return True
    except Exception:
        return False


def extract_audio(data: bytes, *, ffmpeg: str, suffix: str, max_seconds: float, timeout: float) -> bytes | None:
    """Mono 16 kHz Opus (~24 kbit/s) of the first `max_seconds`; None if the file has no audio track."""
    if suffix not in (".mp4", ".webm", ".mov", ".ogg", ".oga", ".mp3", ".m4a"):
        raise MediaRejected("suffix")
    with tempfile.TemporaryDirectory(prefix="bb_audio_") as tmp:
        src = Path(tmp) / f"in{suffix}"
        src.write_bytes(data)
        out = Path(tmp) / "a.ogg"
        cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", *_input_args(suffix),
               "-t", f"{max_seconds:.2f}", "-i", str(src), "-vn", "-sn", "-ac", "1", "-ar", "16000",
               "-c:a", "libopus", "-b:a", "24k", str(out)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False,  # noqa: S603  # nosec B603
                                  env={"PATH": os.environ.get("PATH", "")})
        except subprocess.TimeoutExpired as exc:
            raise MediaRejected("ffmpeg timeout") from exc
        except FileNotFoundError as exc:
            raise MediaRejected("ffmpeg not installed") from exc
        if proc.returncode != 0 or not out.exists() or out.stat().st_size < 200:
            return None  # silent video / no audio stream
        return out.read_bytes()


@dataclass(slots=True)
class TextBoxes:
    index: int
    text_area: float  # fraction of the frame covered by detected text regions
    boxes: int


def detect_text(frames: list[bytes], indices: list[int], *, min_box_frac: float = 0.002,
                max_side: int = OCR_MAX_SIDE) -> list[TextBoxes]:
    """Script-agnostic text DETECTION only (DBNet). Recognition is skipped on purpose: RapidOCR's recognizer
    knows only Latin/CJK and drops boxes it can't read, which made Hindi/Arabic/Korean text look textless."""
    import numpy as np

    eng = _engine()
    det, rec = eng.text_detector, eng.text_recognizer
    out: list[TextBoxes] = []
    for i in indices:
        with Image.open(io.BytesIO(frames[i])) as im:
            img = _fit(im.convert("RGB"), max_side)
        arr = np.asarray(img)
        area = float(img.width * img.height)
        res = det(arr)
        boxes = res[0] if isinstance(res, tuple) else res
        fracs: list[float] = []
        for b in [] if boxes is None else boxes:
            frac = _poly_area(b.tolist()) / area
            if frac < min_box_frac:  # tiny boxes: watermarks, pack credits, noise
                continue
            xs = [int(p[0]) for p in b]
            ys = [int(p[1]) for p in b]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            line_like = w >= 1.3 * h or h >= 1.6 * w  # horizontal line, or vertical CJK column
            if not line_like:
                # Square-ish region: faces/emoji also trigger the detector. Accept only if the recognizer finds
                # glyph-like content (measured: smiley 0.10, single Hindi glyph 0.25, single CJK glyph 0.43).
                crop = np.ascontiguousarray(arr[max(0, min(ys)):max(ys), max(0, min(xs)):max(xs)])
                if crop.size == 0:
                    continue
                r = rec([crop])
                r = r[0] if isinstance(r, tuple) else r
                score = float(r[0][1]) if r else 0.0
                if score < SQUARE_BOX_MIN_REC:
                    continue
            fracs.append(frac)
        out.append(TextBoxes(i, sum(fracs), len(fracs)))
    return out
