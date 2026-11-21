"""Provider-neutral types for translation and vision (OCR) providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

Style = Literal["chat", "formal", "technical", "ocr", "mixed"]


@dataclass(frozen=True, slots=True)
class ContextLine:
    speaker: str  # pseudonymous label ("A", "B"), never a real username
    text: str


@dataclass(slots=True)
class TranslationJob:
    text: str  # Telegram-HTML when html=True
    target: str
    source_hint: str | None = None
    context: tuple[ContextLine, ...] = ()
    reply_to: ContextLine | None = None
    glossary: tuple[tuple[str, str], ...] = ()  # (term, rendering) ; rendering == term means "keep as is"
    style: Style = "chat"
    html: bool = False
    romanized: bool = False
    mixed: bool = False
    unsure: bool = False  # local gate couldn't decide whether translation is needed


@dataclass(slots=True)
class TranslationResult:
    needs_translation: bool
    translation: str
    source_lang: str | None
    confidence: float
    provider: str
    model: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    billed_chars: int = 0
    cached: bool = False


@dataclass(slots=True)
class VisionResult:
    has_text: bool
    text: str
    source_lang: str | None
    translations: dict[str, str]  # target -> translation; missing/empty = already in that language
    confidence: float
    provider: str
    model: str
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class ErrorKind(Enum):
    TRANSIENT = "transient"  # 5xx, connection reset -> retry with backoff
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"  # 429 -> respect retry_after, try fallback
    QUOTA = "quota"  # daily/monthly quota exhausted -> fallback, don't retry today
    AUTH = "auth"  # bad key -> no retry, alert operator
    BAD_REQUEST = "bad_request"  # our bug or unsupported input -> no retry
    BLOCKED = "blocked"  # provider safety filter refused -> fallback provider may succeed
    UNSUPPORTED = "unsupported"  # language pair not supported
    INVALID_OUTPUT = "invalid_output"  # malformed / failed validation


RETRYABLE = frozenset({ErrorKind.TRANSIENT, ErrorKind.TIMEOUT})
FALLBACK_OK = frozenset({ErrorKind.TRANSIENT, ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED, ErrorKind.QUOTA,
                         ErrorKind.BLOCKED, ErrorKind.UNSUPPORTED, ErrorKind.INVALID_OUTPUT, ErrorKind.AUTH})


@dataclass(eq=False)
class ProviderError(Exception):
    kind: ErrorKind
    provider: str
    detail: str = ""  # sanitized; never contains keys or message content
    retry_after: float | None = None
    status: int | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.provider}:{self.kind.value}{f' ({self.status})' if self.status else ''} {self.detail}".strip()


@runtime_checkable
class TranslationProvider(Protocol):
    name: str

    def supports(self, source: str | None, target: str) -> bool: ...

    async def translate(self, job: TranslationJob, *, quality: bool, timeout: float) -> TranslationResult: ...

    async def health_check(self) -> bool: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class VisionProvider(Protocol):
    name: str

    async def read_images(self, images: list[bytes], targets: list[str], *, animated: bool,
                          caption_hint: str | None, timeout: float, audio: bytes | None = None,
                          audio_mime: str | None = None) -> VisionResult: ...
