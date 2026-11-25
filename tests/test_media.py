"""Media pipeline tests on synthetic fixtures (real Pillow/ffmpeg/rlottie/RapidOCR; MOCKED vision + Telegram)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import blank_image, gzip_bomb, lottie_tgs, text_image, video_from_frames
from translator_bot.media import frames as F
from translator_bot.media.processor import MediaProcessor
from translator_bot.metrics import Metrics
from translator_bot.providers.base import ErrorKind, ProviderError, VisionResult
from translator_bot.storage import Storage

pytestmark = pytest.mark.media
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_static_sticker_text_detected() -> None:
    fs = F.load_image(text_image("NO WAY BRO", transparent=True, fmt="WEBP"), 25_000_000)
    [o] = F.ocr_frames(fs.frames, [0])
    assert o.boxes >= 1 and "NO WAY BRO" in o.text.upper()


def test_textless_sticker_has_no_boxes() -> None:
    fs = F.load_image(blank_image(), 25_000_000)
    assert F.ocr_frames(fs.frames, [0])[0].boxes == 0


@pytest.mark.parametrize("kw", [{"noise": True}, {"rotate": 10}, {"font_size": 40, "size": (256, 256)}])
def test_hard_images_still_detected(kw: dict[str, Any]) -> None:
    fs = F.load_image(text_image("HELLO FRIEND", **kw), 25_000_000)
    assert F.ocr_frames(fs.frames, [0])[0].boxes >= 1


def test_decompression_bomb_rejected() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("L", (6000, 6000)).save(buf, format="PNG")
    with pytest.raises(F.MediaRejected):
        F.load_image(buf.getvalue(), max_pixels=1_000_000)


def test_garbage_rejected() -> None:
    with pytest.raises(F.MediaRejected):
        F.load_image(b"not an image at all", 25_000_000)
    with pytest.raises(F.MediaRejected):
        F.render_tgs(b"\x1f\x8bnot gzip", 4_000_000, 10)


def test_tgs_bomb_and_bounds() -> None:
    with pytest.raises(F.MediaRejected, match="gzip bomb"):
        F.render_tgs(gzip_bomb(), 4_000_000, 10)
    import gzip
    import json

    with pytest.raises(F.MediaRejected, match="bounds"):
        F.render_tgs(gzip.compress(json.dumps({"w": 99999, "h": 1, "ip": 0, "op": 1, "fr": 30,
                                               "layers": []}).encode()), 4_000_000, 10)


def test_tgs_renders_and_static_collapses() -> None:
    fs = F.render_tgs(lottie_tgs(), 4_000_000, 24)
    assert fs.animated and len(fs.frames) == 24
    idx = F.select_distinct(fs.frames)
    assert len(idx) <= 3
    assert all(o.boxes == 0 for o in F.ocr_frames(fs.frames, idx))  # textless animation: no text


@needs_ffmpeg
@pytest.mark.parametrize("suffix", [".mp4", ".webm"])
def test_animated_text_sequence(suffix: str) -> None:
    frames = [text_image(t) for t in ("WAIT", "WAIT", "WHAT", "WHAT", "IS THIS", "IS THIS")]
    fs = F.extract_video_frames(video_from_frames(frames, suffix=suffix), ffmpeg="ffmpeg", suffix=suffix,
                                max_seconds=15, max_frames=24, timeout=30)
    idx = F.select_distinct(fs.frames)
    texts = [o.text for o in F.ocr_frames(fs.frames, idx)]
    assert texts == ["WAIT", "WHAT", "IS THIS"]  # each distinct text once, in order


@needs_ffmpeg
def test_brief_text_flash_is_kept() -> None:
    blank = blank_image(fmt="PNG", transparent=False)
    frames = [blank] * 4 + [text_image("BOOM")] + [blank] * 4
    fs = F.extract_video_frames(video_from_frames(frames, seconds_per_frame=0.3), ffmpeg="ffmpeg", suffix=".mp4",
                                max_seconds=15, max_frames=40, timeout=30)
    idx = F.select_distinct(fs.frames)
    assert any("BOOM" in o.text for o in F.ocr_frames(fs.frames, idx))


@needs_ffmpeg
def test_video_limits() -> None:
    frames = [text_image(str(i)) for i in range(30)]
    fs = F.extract_video_frames(video_from_frames(frames, seconds_per_frame=1.0), ffmpeg="ffmpeg", suffix=".mp4",
                                max_seconds=3, max_frames=8, timeout=30)
    assert len(fs.frames) <= 8
    with pytest.raises(F.MediaRejected):
        F.extract_video_frames(b"garbage", ffmpeg="ffmpeg", suffix=".mp4", max_seconds=3, max_frames=8, timeout=10)
    with pytest.raises(F.MediaRejected):
        F.extract_video_frames(b"x", ffmpeg="ffmpeg", suffix=".exe", max_seconds=3, max_frames=8, timeout=10)


# --- processor with fake bot + fake vision --------------------------------------------------------


class FakeFile:
    def __init__(self, size: int) -> None:
        self.file_size, self.file_path = size, "stickers/x.webp"


class FakeBot:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.downloads = 0

    async def get_file(self, file_id: str) -> FakeFile:
        return FakeFile(len(self.data))

    async def download_file(self, path: str, destination: io.BytesIO, timeout: int = 30) -> None:
        self.downloads += 1
        destination.write(self.data)


class FakeVision:
    name = "gemini"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[int] = []
        self.audio: list[bytes | None] = []
        self.targets: list[list[str]] = []
        self.fail = fail

    async def read_images(self, images: list[bytes], targets: list[str], *, animated: bool,
                          caption_hint: str | None, timeout: float, audio: bytes | None = None,
                          audio_mime: str | None = None) -> VisionResult:
        self.calls.append(len(images))
        self.audio.append(audio)
        self.targets.append(list(targets))
        if self.fail:
            raise ProviderError(ErrorKind.QUOTA, "gemini", "x")
        text = "hola a todos, nos vemos mañana" if audio else "NO WAY BRO"
        src = "es" if audio else "en"
        table = {"es": "¡NI DE BROMA!", "hi": "बिल्कुल नहीं भाई", "en": "hi everyone, see you tomorrow" if audio else ""}
        return VisionResult(True, text, src, {t: table.get(t, f"[{t}]") for t in targets if t != src},
                            0.9, "gemini", "m")


@pytest.fixture
async def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "m.sqlite3")
    await s.open()
    yield s
    await s.close()


def processor(bot: Any, storage: Storage, vision: Any) -> MediaProcessor:
    return MediaProcessor(bot, storage, Metrics(), vision=vision, max_download_bytes=8_000_000,
                          max_image_pixels=25_000_000, max_tgs_json_bytes=4_000_000, max_video_seconds=15,
                          max_frames=24, ffmpeg_path="ffmpeg", timeout_s=60, workers=1, ocr_min_confidence=0.6,
                          cache_ttl_s=86400)


async def test_processor_vision_and_cache(storage: Storage) -> None:
    bot, vision = FakeBot(text_image("NO WAY BRO", transparent=True, fmt="WEBP")), FakeVision()
    p = processor(bot, storage, vision)
    try:
        r = await p.extract(file_id="f", file_unique_id="u1", size=None, kind="sticker", targets=["es"])
        assert r is not None and r.translations["es"] == "¡NI DE BROMA!" and r.via == "vision"
        r2 = await p.extract(file_id="f", file_unique_id="u1", size=None, kind="sticker", targets=["es"])
        assert r2 is not None and r2.via == "cache" and bot.downloads == 1 and vision.calls == [1]
    finally:
        await p.close()


async def test_processor_textless_never_calls_vision(storage: Storage) -> None:
    bot, vision = FakeBot(blank_image()), FakeVision()
    p = processor(bot, storage, vision)
    try:
        assert await p.extract(file_id="f", file_unique_id="u2", size=None, kind="sticker", targets=["es"]) is None
        assert vision.calls == []
        assert await p.extract(file_id="f", file_unique_id="u2", size=None, kind="sticker", targets=["es"]) is None
        assert bot.downloads == 1  # negative result cached too
    finally:
        await p.close()


async def test_processor_local_ocr_fallback_when_vision_fails(storage: Storage) -> None:
    p = processor(FakeBot(text_image("HELLO FRIEND")), storage, FakeVision(fail=True))
    try:
        r = await p.extract(file_id="f", file_unique_id="u3", size=None, kind="image", targets=["es"])
        assert r is not None and r.via == "local_ocr" and "HELLO" in r.text.upper()
    finally:
        await p.close()


async def test_processor_rejects_oversize_before_download(storage: Storage) -> None:
    bot = FakeBot(b"x")
    p = processor(bot, storage, FakeVision())
    try:
        assert await p.extract(file_id="f", file_unique_id="u4", size=50_000_000, kind="gif", targets=["es"]) is None
        assert bot.downloads == 0
    finally:
        await p.close()


@needs_ffmpeg
async def test_processor_gif_sends_only_distinct_text_frames(storage: Storage) -> None:
    vid = video_from_frames([text_image(t) for t in ("WAIT", "WAIT", "WHAT", "IS THIS", "IS THIS")])
    vision = FakeVision()
    p = processor(FakeBot(vid), storage, vision)
    try:
        await p.extract(file_id="f", file_unique_id="u5", size=None, kind="gif", targets=["es"])
        assert vision.calls == [3]  # 3 distinct text frames, not dozens
    finally:
        await p.close()


@needs_ffmpeg
def test_extract_audio_and_silent_video() -> None:
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "v.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
        with_audio = out.read_bytes()
        silent = video_from_frames([text_image("HI")] * 3)
    a = F.extract_audio(with_audio, ffmpeg="ffmpeg", suffix=".mp4", max_seconds=60, timeout=30)
    assert a is not None and a[:4] == b"OggS" and len(a) < 40_000
    assert F.extract_audio(silent, ffmpeg="ffmpeg", suffix=".mp4", max_seconds=60, timeout=30) is None


async def test_processor_multi_target_single_call(storage: Storage) -> None:
    vision = FakeVision()
    p = processor(FakeBot(text_image("NO WAY BRO", transparent=True, fmt="WEBP")), storage, vision)
    try:
        r = await p.extract(file_id="f", file_unique_id="mt", size=None, kind="sticker", targets=["es", "hi", "en"])
        assert r is not None and vision.calls == [1]  # ONE call for three languages
        assert r.translations == {"es": "¡NI DE BROMA!", "hi": "बिल्कुल नहीं भाई", "en": ""}  # "" = already English
    finally:
        await p.close()


async def test_processor_voice_sent_as_audio_and_not_persisted(storage: Storage) -> None:
    vision = FakeVision()
    ogg = b"OggS" + b"\x00" * 500
    p = processor(FakeBot(ogg), storage, vision)
    try:
        r = await p.extract(file_id="f", file_unique_id="vo", size=None, kind="voice", targets=["en"], duration=4)
        assert r is not None and r.speech and r.translations["en"] == "hi everyone, see you tomorrow"
        assert vision.audio == [ogg] and vision.calls == [0]
        assert await storage.get_media("vo", 86400) is None  # voice is private content: never cached on disk
    finally:
        await p.close()


async def test_processor_rejects_over_one_minute(storage: Storage) -> None:
    bot, vision = FakeBot(b"x"), FakeVision()
    p = processor(bot, storage, vision)
    try:
        assert await p.extract(file_id="f", file_unique_id="long", size=None, kind="video", targets=["en"],
                               duration=95) is None
        assert bot.downloads == 0 and vision.calls == []
    finally:
        await p.close()


async def test_processor_drops_wrong_script_translation(storage: Storage) -> None:
    class HebrewForHindi(FakeVision):
        async def read_images(self, images: list[bytes], targets: list[str], **kw: Any) -> VisionResult:
            self.calls.append(len(images))
            return VisionResult(True, "BOOM!", "en", {"hi": "בום!", "es": "¡BUM!"}, 0.9, "gemini", "m")

    p = processor(FakeBot(text_image("BOOM!", transparent=True, fmt="WEBP")), storage, HebrewForHindi())
    try:
        r = await p.extract(file_id="f", file_unique_id="ws", size=None, kind="sticker", targets=["hi", "es"])
        assert r is not None and "hi" not in r.translations and r.translations["es"] == "¡BUM!"
    finally:
        await p.close()


@needs_ffmpeg
def test_ffmpeg_never_parses_a_playlist_disguised_as_video(tmp_path: Path) -> None:
    # HLS playlist pointing at a local file: with content probing, ffmpeg would read it (local file disclosure)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n" * 100)
    playlist = (f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nfile://{secret.as_posix()}\n"
                "#EXT-X-ENDLIST\n").encode()
    for suffix in (".mp4", ".webm", ".gif"):
        with pytest.raises(F.MediaRejected):
            F.extract_video_frames(playlist, ffmpeg="ffmpeg", suffix=suffix, max_seconds=5, max_frames=4, timeout=20)
    assert F.extract_audio(playlist, ffmpeg="ffmpeg", suffix=".ogg", max_seconds=5, timeout=20) is None
    assert F._input_args(".mp4")[:6] == ["-protocol_whitelist", "file", "-format_whitelist", "mov", "-f", "mov"]


def test_tgs_with_image_assets_or_layers_is_rejected() -> None:
    import gzip
    import json

    base = json.loads(gzip.decompress(lottie_tgs()))
    image_asset = dict(base, assets=[{"id": "img", "w": 10, "h": 10, "u": "/etc/", "p": "passwd", "e": 0}])
    image_layer = dict(base, layers=[*base["layers"], {"ty": 2, "refId": "img", "ind": 9}])
    nested = dict(base, assets=[{"id": "pre", "layers": [{"ty": 2, "refId": "img"}]}])
    font_file = dict(base, fonts={"list": [{"fName": "x", "fPath": "/etc/passwd"}]})
    for doc in (image_asset, image_layer, nested, font_file):
        with pytest.raises(F.MediaRejected, match="external"):
            F.render_tgs(gzip.compress(json.dumps(doc).encode()), 4_000_000, 4)
    F.render_tgs(lottie_tgs(), 4_000_000, 4)  # ordinary vector sticker still renders


@needs_ffmpeg
async def test_real_gif89a_animation_is_decoded(storage: Storage) -> None:
    # Telegram "animations" are usually MP4 but can be real GIFs; the forced demuxer must follow the bytes
    from PIL import Image

    frames = [Image.open(io.BytesIO(text_image(t))).convert("RGB") for t in ("WAIT", "WHAT")]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=600, loop=0)
    assert buf.getvalue()[:6] == b"GIF89a"
    assert F.sniff_container(buf.getvalue(), ".mp4") == ".gif"
    fs = F.extract_video_frames(buf.getvalue(), ffmpeg="ffmpeg", suffix=".gif", max_seconds=5, max_frames=8,
                                timeout=20)
    assert fs.animated and len(fs.frames) >= 2
    p = processor(FakeBot(buf.getvalue()), storage, FakeVision())
    try:
        r = await p.extract(file_id="f", file_unique_id="gif89", size=None, kind="gif", targets=["es"])
        assert r is not None and r.has_text
    finally:
        await p.close()


async def test_media_translation_word_salad_is_dropped_for_text_retry(storage: Storage) -> None:
    class SaladVision(FakeVision):
        async def read_images(self, *a: Any, **kw: Any) -> VisionResult:
            r = await super().read_images(*a, **kw)
            r.translations["ru"] = "Жестче азец гага Чтев деет завтра?"  # observed live from 3.5-flash-lite
            return r

    p = processor(FakeBot(text_image("NO WAY BRO")), storage, SaladVision())
    try:
        r = await p.extract(file_id="f", file_unique_id="salad", size=None, kind="image", targets=["es", "ru"])
        assert r is not None and r.translations.get("es") == "¡NI DE BROMA!"
        assert "ru" not in r.translations  # missing -> the pipeline's text translator (validated) redoes it
    finally:
        await p.close()


def test_fade_in_caption_frames_are_kept() -> None:
    # small caption fading in over a static character: 64x64 thumbnails averaged it away (live miss)
    from PIL import Image, ImageDraw

    frames = []
    for alpha in (0, 0, 90, 180, 255, 255):
        im = Image.new("RGB", (512, 512), (128, 128, 128))
        d = ImageDraw.Draw(im)
        d.ellipse((150, 200, 360, 410), fill=(250, 200, 60))
        d.text((190, 30), "YALLA", fill=(alpha, alpha, alpha), font=_fixture_font(40))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        frames.append(buf.getvalue())
    idx = F.select_distinct(frames)
    assert idx[0] == 0 and any(i >= 3 for i in idx)  # a frame with the full caption survives


def _fixture_font(size: int) -> Any:
    from tests.fixtures import _font

    return _font(size)


async def test_screen_tag_becomes_icon(storage: Storage) -> None:
    class ScreenVision(FakeVision):
        async def read_images(self, *a: Any, **kw: Any) -> VisionResult:
            return VisionResult(True, "Check this out\n[screen] OFERTA HOY", "en",
                                {"es": "Mira esto\n[screen] OFERTA HOY", "hi": "यह देखो\n[Screen] आज का ऑफर"},
                                0.9, "gemini", "m")

    p = processor(FakeBot(text_image("OFERTA HOY")), storage, ScreenVision())
    try:
        r = await p.extract(file_id="f", file_unique_id="scr", size=None, kind="image", targets=["es", "hi"])
        assert r is not None and "[screen]" not in r.text.lower() and "📺 OFERTA HOY" in r.text
        assert all("[screen]" not in t.lower() and "📺" in t for t in r.translations.values())
    finally:
        await p.close()


@needs_ffmpeg
@pytest.mark.parametrize(("frame_budget", "ocr_budget"), [(4, 6), (12, 2)])
async def test_video_final_caption_survives_sampling(storage: Storage, frame_budget: int, ocr_budget: int) -> None:
    from PIL import Image

    scenes = []
    for level in range(0, 210, 35):
        buf = io.BytesIO()
        Image.new("RGB", (512, 512), (level, level, level)).save(buf, "PNG")
        scenes.append(buf.getvalue())
    scenes.extend([text_image("FINAL WORDS")] * 2)
    video = video_from_frames(scenes, seconds_per_frame=1)
    vision = FakeVision()
    p = processor(FakeBot(video), storage, vision)
    p.max_frames = frame_budget
    p.ocr_gate_frames = ocr_budget
    try:
        result = await p.extract(file_id="last", file_unique_id="late", size=len(video), kind="video",
                                 targets=["es"], duration=8)
        assert result is not None and vision.calls == [1]
    finally:
        await p.close()


@needs_ffmpeg
def test_video_shorter_than_sampling_interval_still_yields_a_frame() -> None:
    clip = video_from_frames([text_image("HI")], seconds_per_frame=0.2)
    frames = F.extract_video_frames(clip, ffmpeg="ffmpeg", suffix=".mp4", max_seconds=60, max_frames=4,
                                    timeout=20, fps=0.5)
    assert len(frames.frames) == 1
    assert F.select_distinct(frames.frames, limit=1) == [0]
