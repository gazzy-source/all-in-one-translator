"""Async media pipeline: download -> render frames / extract audio (process pool) -> text gate ->
ONE Gemini call (reads text + speech, translates into every recipient language) -> cache.

Decision engine (cheapest first):
  1. Same file_unique_id seen before -> reuse cached result (stickers/GIFs repeat constantly).
  2. Size/type/duration validation before download; decompression/gzip bomb guards during decode.
  3. Distinct frames only (static animations collapse to one frame).
  4. Local RapidOCR text gate for silent media: no text boxes -> stop, no provider call.
     Media with audio (video, video notes, voice) always goes to the provider (speech can't be gated locally).
  5. One vision/audio call for all target languages. Without Gemini, RapidOCR text (Latin/CJK only) goes
     through the normal text translator.
Only shared assets (stickers, GIFs) are persisted; photos, videos and voice are private content.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from aiogram import Bot

from translator_bot.languages import LANGUAGES
from translator_bot.media import frames as F
from translator_bot.metrics import Metrics
from translator_bot.providers.base import ProviderError, VisionProvider
from translator_bot.storage import Storage
from translator_bot.textutil import looks_garbled, romanized_output, wrong_script

log = logging.getLogger(__name__)
_SCREEN_TAG = re.compile(r"(?m)^\s*\[\s*screen\s*\]\s*", re.IGNORECASE)

MediaKind = Literal["image", "sticker", "animated_sticker", "video_sticker", "gif", "video", "voice"]
PERSISTED: frozenset[str] = frozenset({"sticker", "animated_sticker", "video_sticker", "gif"})
AUDIO_KINDS: frozenset[str] = frozenset({"video", "voice"})


@dataclass(slots=True)
class MediaText:
    has_text: bool
    text: str
    source_lang: str | None
    confidence: float
    translations: dict[str, str]  # target lang -> translation ("" = already in that language)
    via: str  # "cache" | "vision" | "local_ocr"
    speech: bool = False


class _WorkerTimeout(TimeoutError):
    """A decode/render in the worker pool exceeded its time budget (the pool is recycled)."""


class MediaProcessor:
    def __init__(self, bot: Bot, storage: Storage, metrics: Metrics, *, vision: VisionProvider | None,
                 max_download_bytes: int, max_image_pixels: int, max_tgs_json_bytes: int, max_video_seconds: float,
                 max_frames: int, ffmpeg_path: str, timeout_s: float, workers: int, ocr_min_confidence: float,
                 cache_ttl_s: int, max_media_seconds: float = 60.0, max_video_bytes: int = 20 * 1024 * 1024) -> None:
        self.bot, self.storage, self.metrics, self.vision = bot, storage, metrics, vision
        self.max_download_bytes, self.max_image_pixels = max_download_bytes, max_image_pixels
        self.max_tgs_json_bytes, self.max_video_seconds, self.max_frames = max_tgs_json_bytes, max_video_seconds, max_frames
        self.ffmpeg_path, self.timeout_s, self.ocr_min_confidence = ffmpeg_path, timeout_s, ocr_min_confidence
        self.cache_ttl_s, self.max_media_seconds, self.max_video_bytes = cache_ttl_s, max_media_seconds, max_video_bytes
        self._workers = workers
        self._pool = self._new_pool()
        self._inflight: dict[str, asyncio.Future[MediaText | None]] = {}

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=self._workers, initializer=F.init_worker)

    async def warmup(self) -> None:
        """Spawn workers and load OCR models now, so the first sticker doesn't pay ~3 s of cold start."""
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        results = await asyncio.gather(*(loop.run_in_executor(self._pool, F.warmup) for _ in range(self._workers)),
                                       return_exceptions=True)
        self.metrics.observe("media_warmup_ms", (time.perf_counter() - t0) * 1000)
        log.info("media workers ready", extra={"workers": self._workers, "ocr": any(r is True for r in results)})

    def _recycle_pool(self) -> None:
        old = self._pool
        self._pool = self._new_pool()
        procs = list(getattr(old, "_processes", {}).values())  # private API: best-effort hard stop
        old.shutdown(wait=False, cancel_futures=True)
        for p in procs:
            with contextlib.suppress(Exception):
                p.terminate()
        self.metrics.inc("media.pool_recycled")

    async def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(self._pool, _call, fn, args, kwargs), self.timeout_s)
        except TimeoutError:
            raise _WorkerTimeout from None

    async def _download(self, file_id: str, declared_size: int | None, limit: int) -> bytes:
        if declared_size is not None and declared_size > limit:
            raise F.MediaRejected("too large")
        file = await self.bot.get_file(file_id)
        if file.file_size is not None and file.file_size > limit:
            raise F.MediaRejected("too large")
        if not file.file_path:
            raise F.MediaRejected("no file path")
        buf = io.BytesIO()
        await self.bot.download_file(file.file_path, destination=buf, timeout=int(self.timeout_s))
        data = buf.getvalue()
        if len(data) > limit:
            raise F.MediaRejected("too large")
        return data

    async def extract(self, *, file_id: str, file_unique_id: str, size: int | None, kind: MediaKind,
                      targets: list[str], caption_hint: str | None = None,
                      duration: float | None = None) -> MediaText | None:
        """Text/speech found in the media with translations into `targets` (when available), or None."""
        if kind in AUDIO_KINDS and duration is not None and duration > self.max_media_seconds:
            self.metrics.inc(f"media.too_long.{kind}")
            log.info("media skipped: too long", extra={"kind": kind, "seconds": duration,
                                                       "limit": self.max_media_seconds})
            return None
        cached = await self.storage.get_media(file_unique_id, self.cache_ttl_s) if kind in PERSISTED else None
        if cached is not None:
            self.metrics.inc("media.cache_hit")
            if not cached["has_text"]:
                return None
            # languages missing from the cache are filled in by the text translator
            return MediaText(True, cached["text"] or "", cached["source_lang"], float(cached["confidence"] or 0),
                             dict(cached["translations"]), "cache")
        key = f"{file_unique_id}:{','.join(sorted(targets))}"
        while (fut := self._inflight.get(key)) is not None:
            try:
                return await asyncio.shield(fut)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
                # the owner was cancelled (its queue job timed out): do the work ourselves
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            res = await self._extract_uncached(file_id, file_unique_id, size, kind, targets, caption_hint)
            fut.set_result(res)
            return res
        except asyncio.CancelledError:
            fut.cancel()  # waiters retry instead of inheriting our cancellation
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    def _stage(self, name: str, t0: float) -> float:
        now = time.perf_counter()
        self.metrics.observe(f"media_stage_ms.{name}", (now - t0) * 1000)
        return now

    async def _extract_uncached(self, file_id: str, file_unique_id: str, size: int | None, kind: MediaKind,
                                targets: list[str], caption_hint: str | None) -> MediaText | None:
        self.metrics.inc(f"media.process.{kind}")
        t_start = t = time.perf_counter()
        persist = kind in PERSISTED
        audio: bytes | None = None
        audio_mime: str | None = None
        fs: F.FrameSet | None = None
        images: list[bytes] = []
        text_idx: list[int] = []
        try:
            limit = self.max_video_bytes if kind in ("video", "voice", "gif") else self.max_download_bytes
            data = await self._download(file_id, size, limit)
            t = self._stage("download", t)
            if kind in ("image", "sticker"):
                fs = await self._run(F.load_image, data, self.max_image_pixels)
            elif kind == "animated_sticker":
                fs = await self._run(F.render_tgs, data, self.max_tgs_json_bytes, self.max_frames)
            elif kind == "voice":
                # The declared duration comes from the sender's client, so it can lie: only send small files as-is
                # (<= 64 kbit/s for the allowed length); anything bigger is trimmed by ffmpeg (-t limit).
                if len(data) <= self.max_media_seconds * 8000 and data[:4] == b"OggS":
                    audio, audio_mime = data, "audio/ogg"  # Telegram voice is already small Opus: send as-is
                else:
                    audio = await self._run(F.extract_audio, data, ffmpeg=self.ffmpeg_path,
                                            suffix=F.sniff_container(data, ".ogg"),
                                            max_seconds=self.max_media_seconds, timeout=self.timeout_s - 2)
                    audio_mime = "audio/ogg"
            else:
                seconds = self.max_media_seconds if kind == "video" else self.max_video_seconds
                suffix = F.sniff_container(data, ".webm" if kind == "video_sticker" else ".mp4")
                if suffix not in (".webm", ".mp4", ".gif", ".avi"):
                    raise F.MediaRejected("not a video container")
                fps = 1.0 if kind == "video" else 4.0
                fs = await self._run(F.extract_video_frames, data, ffmpeg=self.ffmpeg_path, suffix=suffix,
                                     max_seconds=seconds, max_frames=self.max_frames, timeout=self.timeout_s - 2,
                                     fps=fps)
                if kind == "video" and suffix != ".gif":  # GIFs carry no audio
                    audio = await self._run(F.extract_audio, data, ffmpeg=self.ffmpeg_path, suffix=suffix,
                                            max_seconds=self.max_media_seconds, timeout=self.timeout_s - 2)
                    audio_mime = "audio/ogg"
            del data
            t = self._stage("decode", t)
            if fs is not None:
                distinct: list[int] = await self._run(F.select_distinct, fs.frames)
                gate = distinct if len(distinct) <= 6 else [distinct[int(i * len(distinct) / 6)] for i in range(6)]
                found: list[F.TextBoxes] = await self._run(F.detect_text, fs.frames, gate)
                t = self._stage("ocr_gate", t)
                text_idx = [b.index for b in found if b.boxes > 0 and b.text_area >= 0.004]
                if len(text_idx) > 4:  # keep first/last appearance and evenly spaced frames in between
                    text_idx = [text_idx[round(k * (len(text_idx) - 1) / 3)] for k in range(4)]
                images = [fs.frames[i] for i in text_idx]
        except (F.MediaRejected, TimeoutError) as exc:
            if isinstance(exc, _WorkerTimeout):
                # a hung native render (rlottie/ffmpeg) must not occupy a worker forever. Only worker timeouts:
                # a slow *download* recycling the pool used to kill other jobs' healthy renders.
                self._recycle_pool()
            self.metrics.inc(f"media.rejected.{type(exc).__name__}")
            log.info("media rejected", extra={"kind": kind, "reason": str(exc)[:60]})
            return None
        except Exception:
            self.metrics.inc("media.error")
            log.exception("media processing failed", extra={"kind": kind})
            return None

        if not images and not audio:
            self.metrics.inc("media.no_text")
            if persist:
                await self.storage.put_media(file_unique_id, has_text=False, text=None, source_lang=None,
                                             confidence=0, translations={})
            self.metrics.observe(f"media_ms.{kind}", (time.perf_counter() - t_start) * 1000)
            return None

        if self.vision is not None:
            try:
                vr = await self.vision.read_images(
                    images, targets, animated=bool(fs and fs.animated and len(images) > 1), caption_hint=caption_hint,
                    timeout=min(30.0 if audio else 20.0, self.timeout_s), audio=audio, audio_mime=audio_mime)
                self._stage("vision", t)
                self.metrics.inc("media.vision")
                self.metrics.observe(f"media_ms.{kind}", (time.perf_counter() - t_start) * 1000)
                if not vr.has_text or vr.confidence < self.ocr_min_confidence or not vr.text:
                    self.metrics.inc(f"media.no_text.{kind}")
                    # reason only, never the content
                    log.info("media: nothing to translate", extra={
                        "kind": kind, "has_text": vr.has_text, "confidence": round(vr.confidence, 2),
                        "chars": len(vr.text or ""), "min_confidence": self.ocr_min_confidence})
                    if persist:
                        await self.storage.put_media(file_unique_id, has_text=False, text=None, source_lang=None,
                                                     confidence=vr.confidence, translations={})
                    return None
                # the prompt marks on-screen text in videos with "[screen]"; show readers an icon, not a tag
                vr.text = _SCREEN_TAG.sub("📺 ", vr.text)
                vr.translations = {k: _SCREEN_TAG.sub("📺 ", v) for k, v in vr.translations.items()}
                # "" = already in that language, so the text path doesn't redo it. A translation in the wrong
                # script (e.g. Hebrew letters for a Hindi reader) is dropped: the text translator redoes it.
                translations: dict[str, str] = {}
                for tg in targets:
                    tr = vr.translations.get(tg, "")
                    lang = LANGUAGES.get(tg)
                    if tr and lang is not None and (wrong_script(tr, lang.script)
                                                    or romanized_output(vr.text, tr, lang.script, tg)):
                        self.metrics.inc("media.wrong_script")
                        continue
                    if tr and looks_garbled(vr.text, tr, tg):  # word salad: the text translator redoes it
                        self.metrics.inc("media.garbled")
                        continue
                    translations[tg] = tr
                if persist:
                    await self.storage.put_media(file_unique_id, has_text=True, text=vr.text,
                                                 source_lang=vr.source_lang, confidence=vr.confidence,
                                                 translations=translations)
                return MediaText(True, vr.text, vr.source_lang, vr.confidence, translations, "vision",
                                 speech=bool(audio))
            except ProviderError as err:
                self.metrics.inc(f"media.vision_error.{err.kind.value}")
                log.warning("vision failed; using local OCR", extra={"kind": err.kind.value})

        # Local OCR fallback (no speech recognition; RapidOCR reads Latin/CJK only): trust confident reads.
        self.metrics.observe(f"media_ms.{kind}", (time.perf_counter() - t_start) * 1000)
        if fs is None or not text_idx:
            return None
        try:
            ocr = await self._run(F.ocr_frames, fs.frames, text_idx)
        except Exception:
            return None
        texts: list[str] = []
        for o in ocr:  # drop repeats (text that stays on screen across frames)
            if o.text and o.text not in texts:
                texts.append(o.text)
        scored = [o.score for o in ocr if o.text]
        score = sum(scored) / len(scored) if scored else 0.0
        text = " / ".join(texts)
        if not text or score < self.ocr_min_confidence:
            self.metrics.inc("media.low_confidence")
            return None
        self.metrics.inc("media.local_ocr")
        return MediaText(True, text, None, score, {}, "local_ocr")


def _call(fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    return fn(*args, **kwargs)
