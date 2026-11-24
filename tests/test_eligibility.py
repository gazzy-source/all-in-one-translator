from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import BOT_ID, group_message, to_message
from translator_bot.eligibility import Kind, Skip, classify
from translator_bot.storage import DEFAULT_CHAT_SETTINGS

S = dict(DEFAULT_CHAT_SETTINGS)


def c(d: dict[str, Any], settings: dict[str, Any] | None = None, media: bool = True) -> Kind | Skip:
    r = classify(to_message(d), bot_id=BOT_ID, settings=settings or S, media_enabled=media)
    return r if isinstance(r, Skip) else r.kind


STICKER = {"file_id": "f", "file_unique_id": "u", "type": "regular", "width": 512, "height": 512,
           "is_animated": False, "is_video": False}


@pytest.mark.parametrize(("msg", "expected"), [
    (group_message("hola amigos"), Kind.TEXT),
    (group_message("/play အောင်မြင့်မြတ်သီချင်းကောင်းများ"), Kind.TEXT),
    (group_message("/play"), Skip.COMMAND),
    (group_message("/start"), Skip.COMMAND),
    (group_message("😂😂"), Skip.NON_LINGUISTIC),
    (group_message("https://x.io"), Skip.NON_LINGUISTIC),
    (group_message("def f(x):\n    return x;"), Skip.NON_LINGUISTIC),
    (group_message("hola", is_bot=True, sender=999), Skip.BOT_AUTHOR),
    (group_message("hola", sender=BOT_ID, is_bot=True), Skip.BOT_AUTHOR),
    (group_message("hola", via_bot=BOT_ID), Skip.OWN_INLINE),
    (group_message("hola", extra={"ephemeral_message_id": 3}), Skip.SERVICE),
    (group_message(extra={"sticker": STICKER}), Kind.STICKER),
    (group_message(extra={"sticker": {**STICKER, "is_animated": True}}), Kind.ANIMATED_STICKER),
    (group_message(extra={"sticker": {**STICKER, "is_video": True}}), Kind.VIDEO_STICKER),
    (group_message(extra={"animation": {"file_id": "a", "file_unique_id": "b", "width": 1, "height": 1,
                                        "duration": 1}}), Kind.GIF),
    (group_message(extra={"photo": [{"file_id": "p", "file_unique_id": "q", "width": 9, "height": 9}]}), Kind.IMAGE),
    (group_message(caption="mira esto", extra={"photo": [{"file_id": "p", "file_unique_id": "q", "width": 9,
                                                         "height": 9}]}), Kind.CAPTION),
    (group_message(caption="mira esto", extra={"document": {"file_id": "d", "file_unique_id": "e"}}), Kind.CAPTION),
    (group_message(extra={"voice": {"file_id": "v", "file_unique_id": "w", "duration": 2}}), Kind.VOICE),
    (group_message(extra={"video": {"file_id": "v", "file_unique_id": "w", "width": 1, "height": 1,
                                    "duration": 20}}), Kind.VIDEO),
    (group_message(extra={"video_note": {"file_id": "v", "file_unique_id": "w", "length": 240,
                                         "duration": 9}}), Kind.VIDEO),
    (group_message(extra={"document": {"file_id": "d", "file_unique_id": "e"}}), Skip.NO_CONTENT),
    (group_message(extra={"new_chat_members": [{"id": 5, "is_bot": False, "first_name": "x"}]}), Skip.SERVICE),
])
def test_classify(msg: dict[str, Any], expected: Kind | Skip) -> None:
    assert c(msg) == expected


def test_media_toggles() -> None:
    off = {**S, "stickers": False, "gifs": False, "images": False}
    assert c(group_message(extra={"sticker": STICKER}), off) is Skip.MEDIA_DISABLED
    photo = {"photo": [{"file_id": "p", "file_unique_id": "q", "width": 9, "height": 9}]}
    assert c(group_message(caption="hola amigos", extra=photo), off) is Kind.CAPTION
    assert c(group_message(extra={"sticker": STICKER}), media=False) is Skip.MEDIA_DISABLED


def test_translate_bots_opt_in() -> None:
    assert c(group_message("hola", sender=999, is_bot=True), {**S, "translate_bots": True}) is Kind.TEXT
    # but never the bot itself
    assert c(group_message("hola", sender=BOT_ID, is_bot=True), {**S, "translate_bots": True}) is Skip.BOT_AUTHOR
