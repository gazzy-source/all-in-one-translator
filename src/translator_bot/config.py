"""Application configuration, loaded from environment variables / .env and validated at startup."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised with an actionable message when configuration is invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -------------------------------------------------------------------------------
    bot_token: SecretStr = Field(..., alias="BOT_TOKEN")
    run_mode: Literal["polling", "webhook"] = Field("polling", alias="RUN_MODE")
    webhook_url: str | None = Field(None, alias="WEBHOOK_URL")  # public https URL incl. path
    webhook_secret: SecretStr | None = Field(None, alias="WEBHOOK_SECRET")
    listen_host: str = Field("127.0.0.1", alias="LISTEN_HOST")
    listen_port: int = Field(8080, alias="LISTEN_PORT")
    owner_ids: Annotated[list[int], NoDecode] = Field(default_factory=list, alias="OWNER_IDS")

    # --- Providers ------------------------------------------------------------------------------
    gemini_api_key: SecretStr | None = Field(None, alias="GEMINI_API_KEY")
    gemini_fast_model: str = Field("gemini-3.1-flash-lite", alias="GEMINI_FAST_MODEL")
    # Defaults chosen from live measurements on the free tier (2026-11-21): larger Flash models took 10-40 s or
    # returned 503, so only flash-lite models are practical. For text, 3.1-flash-lite: in a 27-sample A/B of
    # slang into ru/ar/hi, 3.5-flash-lite garbled 3-5 outputs (word salad, wrong script, cruder slang) at
    # temperature 0 and 0.2; 3.1-flash-lite garbled none. "Quality" = same model with more thinking.
    gemini_quality_model: str = Field("gemini-3.1-flash-lite", alias="GEMINI_QUALITY_MODEL")
    gemini_fallback_model: str | None = Field("gemini-3.5-flash-lite", alias="GEMINI_FALLBACK_MODEL")
    gemini_fast_thinking: str = Field("MINIMAL", alias="GEMINI_FAST_THINKING")
    gemini_quality_thinking: str = Field("LOW", alias="GEMINI_QUALITY_THINKING")
    gemini_vision_model: str = Field("gemini-3.5-flash-lite", alias="GEMINI_VISION_MODEL")
    gemini_rpm: int = Field(15, alias="GEMINI_RPM", ge=1)  # local guard, per model
    gemini_rpd: int = Field(1000, alias="GEMINI_RPD", ge=1)  # local guard, per model per day
    gemini_store: bool = Field(False, alias="GEMINI_STORE")
    gemini_base_url: str = Field("https://generativelanguage.googleapis.com/v1beta", alias="GEMINI_BASE_URL")

    deepl_api_key: SecretStr | None = Field(None, alias="DEEPL_API_KEY")
    deepl_monthly_char_budget: int = Field(950_000, alias="DEEPL_MONTHLY_CHAR_BUDGET", ge=0)
    deepl_rpm: int = Field(60, alias="DEEPL_RPM", ge=1)

    provider_timeout_s: float = Field(12.0, alias="PROVIDER_TIMEOUT_S", gt=0)
    inline_timeout_s: float = Field(4.0, alias="INLINE_TIMEOUT_S", gt=0)

    # --- Behaviour ------------------------------------------------------------------------------
    default_target_lang: str = Field("en", alias="DEFAULT_TARGET_LANG")
    context_messages: int = Field(6, alias="CONTEXT_MESSAGES", ge=0, le=20)
    context_ttl_s: int = Field(900, alias="CONTEXT_TTL_S", ge=0)
    max_text_chars: int = Field(3000, alias="MAX_TEXT_CHARS", ge=50)
    # Language-gate word lists: "best" (~1 GB RAM once all scripts are seen) or "small" (~0.1 GB, for 1 GB hosts;
    # same benchmark scores, ~1% of gate decisions differ on code-switched/slang lines - see PLAN.md L16)
    detector_wordlist: Literal["best", "small"] = Field("best", alias="DETECTOR_WORDLIST")
    message_map_retention_h: int = Field(48, alias="MESSAGE_MAP_RETENTION_H", ge=1)
    cache_ttl_days: int = Field(30, alias="CACHE_TTL_DAYS", ge=0)
    edit_window_s: int = Field(48 * 3600, alias="EDIT_WINDOW_S", ge=0)

    # --- Media ----------------------------------------------------------------------------------
    media_enabled: bool = Field(True, alias="MEDIA_ENABLED")
    max_download_bytes: int = Field(8 * 1024 * 1024, alias="MAX_DOWNLOAD_BYTES", ge=1024)
    max_image_pixels: int = Field(25_000_000, alias="MAX_IMAGE_PIXELS", ge=10_000)
    max_tgs_json_bytes: int = Field(4 * 1024 * 1024, alias="MAX_TGS_JSON_BYTES", ge=1024)
    max_video_seconds: float = Field(15.0, alias="MAX_VIDEO_SECONDS", gt=0)  # GIFs / video stickers
    max_media_seconds: float = Field(60.0, alias="MAX_MEDIA_SECONDS", gt=0, le=300)  # videos / voice (speech)
    max_video_bytes: int = Field(20 * 1024 * 1024, alias="MAX_VIDEO_BYTES", ge=1024)  # Telegram getFile cap: 20 MB
    max_frames: int = Field(24, alias="MAX_FRAMES", ge=1, le=120)
    ffmpeg_path: str = Field("ffmpeg", alias="FFMPEG_PATH")
    media_timeout_s: float = Field(30.0, alias="MEDIA_TIMEOUT_S", ge=5)  # ffmpeg gets this minus 2 s
    media_workers: int = Field(2, alias="MEDIA_WORKERS", ge=1, le=16)
    ocr_min_confidence: float = Field(0.6, alias="OCR_MIN_CONFIDENCE", ge=0, le=1)
    # frames of a GIF/video the local text detector checks before any AI call (fewer = faster on slow CPUs)
    ocr_gate_frames: int = Field(6, alias="OCR_GATE_FRAMES", ge=1, le=12)
    vision_ocr: bool = Field(True, alias="VISION_OCR")  # use Gemini vision when key present

    # --- Throughput -----------------------------------------------------------------------------
    text_workers: int = Field(4, alias="TEXT_WORKERS", ge=1, le=64)
    queue_max: int = Field(500, alias="QUEUE_MAX", ge=10)
    per_chat_queue_max: int = Field(40, alias="PER_CHAT_QUEUE_MAX", ge=1)
    # Ephemeral-message rate limits are not documented by Telegram; this is a conservative local cap.
    ephemeral_per_chat_per_min: int = Field(60, alias="EPHEMERAL_PER_CHAT_PER_MIN", ge=1)
    global_sends_per_s: int = Field(25, alias="GLOBAL_SENDS_PER_S", ge=1)  # Telegram: ~30/s
    inline_per_min: int = Field(30, alias="INLINE_PER_MIN", ge=1)
    private_per_min: int = Field(12, alias="PRIVATE_PER_MIN", ge=1)  # 1:1 translations per user per minute
    # 1:1 translations per day for users who are not approved (0 = unlimited); keeps free-tier quota for groups
    private_daily_limit: int = Field(200, alias="PRIVATE_DAILY_LIMIT", ge=0)
    # ... and in total across all of them (0 = unlimited). In memory: a restart resets both counters.
    private_global_daily_limit: int = Field(500, alias="PRIVATE_GLOBAL_DAILY_LIMIT", ge=0)
    burst_window_ms: int = Field(700, alias="BURST_WINDOW_MS", ge=0, le=5000)
    failure_digest_minutes: int = Field(30, alias="FAILURE_DIGEST_MINUTES", ge=1)

    # --- Access -------------------------------------------------------------------------------
    onboarding_required: bool = Field(False, alias="ONBOARDING_REQUIRED")
    inline_access: Literal["approved", "everyone"] = Field("approved", alias="INLINE_ACCESS")
    # who may use the 1:1 translator in a private chat with the bot (group translations always need approval)
    private_access: Literal["approved", "everyone"] = Field("everyone", alias="PRIVATE_ACCESS")
    enable_new_groups: Literal["owner_added", "all", "none"] = Field("owner_added", alias="ENABLE_NEW_GROUPS")

    # --- Storage / logging ----------------------------------------------------------------------
    database_path: Path = Field(Path("data/translator.sqlite3"), alias="DATABASE_PATH")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_json: bool = Field(False, alias="LOG_JSON")
    health_port: int | None = Field(None, alias="HEALTH_PORT")  # polling mode health endpoint

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _parse_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("default_target_lang")
    @classmethod
    def _lang(cls, v: str) -> str:
        from translator_bot.languages import resolve_language

        lang = resolve_language(v)
        if lang is None:
            raise ValueError(f"unknown language {v!r}")
        return lang.code

    @model_validator(mode="after")
    def _check(self) -> Settings:
        token = self.bot_token.get_secret_value()
        if not re.fullmatch(r"\d{3,20}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("BOT_TOKEN does not look like a BotFather token (expected '<numeric id>:<secret>')")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
        if self.edit_window_s > self.message_map_retention_h * 3600:
            # once delivery rows are purged an edit can't find the ephemeral and would send a duplicate
            raise ValueError("EDIT_WINDOW_S must not exceed MESSAGE_MAP_RETENTION_H * 3600")
        if self.run_mode == "webhook":
            if not self.webhook_url or not self.webhook_url.startswith("https://"):
                raise ValueError("RUN_MODE=webhook requires WEBHOOK_URL starting with https://")
            if not self.webhook_secret or len(self.webhook_secret.get_secret_value()) < 16:
                raise ValueError("RUN_MODE=webhook requires WEBHOOK_SECRET (>=16 chars, [A-Za-z0-9_-])")
        if not self.owner_ids:
            raise ValueError("OWNER_IDS is required: your numeric Telegram user ID (comma-separated for several "
                             "owners). Get it from @userinfobot or the bot's access-denied message.")
        if not self.gemini_api_key and not self.deepl_api_key:
            raise ValueError("configure at least one provider: GEMINI_API_KEY and/or DEEPL_API_KEY")
        return self

    @cached_property
    def bot_id(self) -> int:
        return int(self.bot_token.get_secret_value().split(":", 1)[0])


def load_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # pydantic ValidationError -> actionable message, no secret values
        lines = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            for err in errors():
                loc = ".".join(str(p) for p in err.get("loc", ())) or "settings"
                lines.append(f"  - {loc}: {err.get('msg')}")
        detail = "\n".join(lines) or f"  - {type(exc).__name__}"
        raise ConfigError(f"Invalid configuration (see .env.example):\n{detail}") from None
