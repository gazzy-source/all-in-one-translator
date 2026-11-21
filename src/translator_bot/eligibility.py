"""Decide whether an incoming group message should be processed, and how. Pure and fast."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from aiogram.types import Message

from translator_bot.textutil import ContentKind, profile_text


class Skip(Enum):
    NOT_GROUP = "not_group"
    CHAT_DISABLED = "chat_disabled"
    BOT_AUTHOR = "bot_author"
    OWN_INLINE = "own_inline_result"
    SERVICE = "service"
    COMMAND = "command"
    NO_CONTENT = "no_content"
    NON_LINGUISTIC = "non_linguistic"
    MEDIA_DISABLED = "media_disabled"
    UNSUPPORTED = "unsupported"


class Kind(Enum):
    TEXT = "text"
    CAPTION = "caption"  # photo/video/document/... with caption (photo also OCR'd if images enabled)
    IMAGE = "image"  # photo without caption
    STICKER = "sticker"  # static webp
    ANIMATED_STICKER = "animated_sticker"  # TGS (Lottie)
    VIDEO_STICKER = "video_sticker"  # WEBM
    GIF = "gif"  # animation (MP4)
    VIDEO = "video"  # video / round video note: speech + on-screen text
    VOICE = "voice"  # voice message / audio file: speech


@dataclass(frozen=True, slots=True)
class Eligible:
    kind: Kind
    text_html: str  # text or caption as Telegram-HTML ("" for pure media)
    plain: str
    has_media: bool


def sender_name(msg: Message) -> str:
    if msg.sender_chat is not None:  # anonymous admin / channel post in discussion group
        return msg.sender_chat.title or "Channel"
    if msg.from_user is not None:
        return msg.from_user.full_name
    return "Someone"


def sender_id(msg: Message) -> int | None:
    if msg.sender_chat is not None:
        return msg.sender_chat.id
    return msg.from_user.id if msg.from_user else None


def classify(msg: Message, *, bot_id: int, settings: dict[str, Any], media_enabled: bool) -> Eligible | Skip:
    if msg.chat.type not in ("group", "supergroup"):
        return Skip.NOT_GROUP
    if msg.via_bot is not None and msg.via_bot.id == bot_id:
        return Skip.OWN_INLINE  # our own inline result posted by a user: never re-translate
    if msg.from_user is not None and msg.from_user.is_bot and msg.sender_chat is None:
        if msg.from_user.id == bot_id or not settings.get("translate_bots", False):
            return Skip.BOT_AUTHOR
    if msg.ephemeral_message_id is not None:
        return Skip.SERVICE  # an ephemeral message (e.g. hidden command) is not group conversation

    text = msg.text
    caption = msg.caption
    if text is not None:
        if text.lstrip().startswith("/"):
            return Skip.COMMAND
        prof = profile_text(text)
        if prof.kind not in (ContentKind.TEXT, ContentKind.CODE):
            return Skip.NON_LINGUISTIC
        if prof.kind is ContentKind.CODE:
            return Skip.NON_LINGUISTIC  # code is not translated
        if not settings.get("text", True):
            return Skip.CHAT_DISABLED
        return Eligible(Kind.TEXT, msg.html_text, text, False)

    cap_ok = caption is not None and settings.get("captions", True) and \
        profile_text(caption).kind is ContentKind.TEXT
    cap_html = msg.html_text if cap_ok else ""
    cap_plain = caption if cap_ok and caption else ""

    if msg.sticker is not None:
        st = msg.sticker
        if not media_enabled:
            return Skip.MEDIA_DISABLED
        if st.is_animated:
            return Eligible(Kind.ANIMATED_STICKER, "", "", True) if settings.get("animated", True) else Skip.MEDIA_DISABLED
        if st.is_video:
            return Eligible(Kind.VIDEO_STICKER, "", "", True) if settings.get("animated", True) else Skip.MEDIA_DISABLED
        return Eligible(Kind.STICKER, "", "", True) if settings.get("stickers", True) else Skip.MEDIA_DISABLED
    if msg.animation is not None:
        if media_enabled and settings.get("gifs", True):
            return Eligible(Kind.GIF, cap_html, cap_plain, True)
        return Eligible(Kind.CAPTION, cap_html, cap_plain, False) if cap_ok else Skip.MEDIA_DISABLED
    if msg.photo:
        if media_enabled and settings.get("images", True):
            return Eligible(Kind.IMAGE if not cap_ok else Kind.CAPTION, cap_html, cap_plain, True)
        return Eligible(Kind.CAPTION, cap_html, cap_plain, False) if cap_ok else Skip.MEDIA_DISABLED
    if msg.video is not None or msg.video_note is not None:
        if media_enabled and settings.get("videos", True):
            return Eligible(Kind.VIDEO, cap_html, cap_plain, True)
        return Eligible(Kind.CAPTION, cap_html, cap_plain, False) if cap_ok else Skip.MEDIA_DISABLED
    if msg.voice is not None or msg.audio is not None:
        if media_enabled and settings.get("voice", True):
            return Eligible(Kind.VOICE, cap_html, cap_plain, True)
        return Eligible(Kind.CAPTION, cap_html, cap_plain, False) if cap_ok else Skip.MEDIA_DISABLED
    if cap_ok:  # document etc. with caption: caption only
        return Eligible(Kind.CAPTION, cap_html, cap_plain, False)
    if msg.new_chat_members or msg.left_chat_member or msg.pinned_message or msg.new_chat_title:
        return Skip.SERVICE
    if caption is not None or msg.video or msg.document or msg.voice or msg.audio or msg.video_note:
        return Skip.NO_CONTENT
    return Skip.UNSUPPORTED
