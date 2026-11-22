"""End-to-end (MOCKED Telegram/providers) for media in groups and for inline mode."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.types import Update

from tests.conftest import (
    BOT_ID,
    GROUP_ID,
    HINDI,
    OUTSIDER,
    OWNER,
    SPANISH_SPEAKER,
    FakeProvider,
    FakeSession,
    FakeTelegram,
    group_message,
)
from tests.fixtures import blank_image, text_image
from tests.test_e2e_group import Harness, make_settings
from tests.test_media import FakeVision
from translator_bot.app import App
from translator_bot.handlers import build_routers


@pytest.fixture
async def mh(tmp_path: Path, detector: Any) -> Any:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, HINDI, OUTSIDER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    vision = FakeVision()
    app = App(make_settings(tmp_path, MEDIA_ENABLED=True, MEDIA_WORKERS=1), session=FakeSession(tg),
              providers={"gemini": gemini}, detector=detector, vision=vision)
    await app.start()
    await app.storage.upsert_chat(GROUP_ID, title="Test Group", enabled=True, bot_is_admin=True)
    await app.storage.upsert_user(OWNER, lang="es", onboarded=True)
    dp = Dispatcher()
    for r in build_routers(app):
        dp.include_router(r)
    h = Harness(app, dp, tg, gemini, gemini)
    h.vision = vision  # type: ignore[attr-defined]
    yield h
    await app.stop()


def sticker(fid: str) -> dict[str, Any]:
    return {"sticker": {"file_id": fid, "file_unique_id": f"uniq-{fid}", "type": "regular", "width": 512,
                        "height": 512, "is_animated": False, "is_video": False}}


@pytest.mark.media
async def test_sticker_text_translated_privately(mh: Harness) -> None:
    mh.tg.files["st1"] = text_image("NO WAY BRO", transparent=True, fmt="WEBP")
    await mh.send(group_message(sender=SPANISH_SPEAKER, name="Maria", extra=sticker("st1")))
    await mh.idle(timeout=60)
    sent = mh.tg.ephemeral_to(OWNER)
    assert len(sent) == 1 and "¡NI DE BROMA!" in sent[0].params["text"] and "sticker" in sent[0].params["text"]
    assert mh.tg.public_group_output() == []
    # the same sticker again: cached, no new download, no new vision call
    await mh.send(group_message(sender=SPANISH_SPEAKER, extra=sticker("st1")))
    await mh.idle(timeout=60)
    assert len(mh.tg.ephemeral_to(OWNER)) == 2
    assert mh.vision.calls == [1]  # type: ignore[attr-defined]
    assert sum(1 for c in mh.tg.calls if c.method == "GetFile") == 1


@pytest.mark.media
async def test_sticker_translated_in_private_chat(mh: Harness) -> None:
    from tests.test_e2e_group import dm

    await mh.app.storage.upsert_user(OUTSIDER, lang="es")
    mh.tg.files["st9"] = text_image("NO WAY BRO", transparent=True, fmt="WEBP")
    await mh.send(dm(OUTSIDER, mid=21, **sticker("st9")))
    mh.tg.files["st10"] = blank_image()
    await mh.send(dm(OUTSIDER, mid=22, **sticker("st10")))
    await mh.idle(timeout=60)
    a, b = mh.tg.dms_to(OUTSIDER)
    assert "¡NI DE BROMA!" in a.params["text"] and "English → Spanish" in a.params["text"]
    assert "couldn't find any text" in b.params["text"]
    assert mh.tg.public_group_output() == []


@pytest.mark.media
async def test_private_media_timeout_says_slow_and_keeps_pool(mh: Harness) -> None:
    """Live 2026-11-22 (1/8-core VM): two GIFs hit the 30 s worker timeout; the user was told "no text", and the
    pool recycle made the next job reload the OCR model and time out as well."""
    from tests.test_e2e_group import dm
    from translator_bot.media.processor import _WorkerTimeout

    proc = mh.app.media
    assert proc is not None
    recycled: list[bool] = []

    async def slow(fn: Any, *a: Any, **k: Any) -> Any:
        raise _WorkerTimeout(fn.__name__)

    proc._run = slow  # type: ignore[method-assign]
    proc._recycle_pool = lambda: recycled.append(True)  # type: ignore[method-assign]
    mh.tg.files["st11"] = text_image("NO WAY BRO", transparent=True, fmt="WEBP")
    await mh.send(dm(OUTSIDER, mid=31, **sticker("st11")))
    await mh.idle(timeout=60)
    [out] = mh.tg.dms_to(OUTSIDER)
    assert "took too long" in out.params["text"] and recycled == []


@pytest.mark.media
async def test_textless_sticker_silent(mh: Harness) -> None:
    mh.tg.files["st2"] = blank_image()
    await mh.send(group_message(sender=SPANISH_SPEAKER, extra=sticker("st2")))
    await mh.idle(timeout=60)
    assert mh.tg.ephemeral_to(OWNER) == [] and mh.vision.calls == []  # type: ignore[attr-defined]


@pytest.mark.media
async def test_already_target_language_sticker_silent(mh: Harness) -> None:
    await mh.app.storage.upsert_user(OWNER, lang="en")  # FakeVision says the sticker text is English
    mh.tg.files["st3"] = text_image("NO WAY BRO", transparent=True, fmt="WEBP")
    await mh.send(group_message(sender=SPANISH_SPEAKER, extra=sticker("st3")))
    await mh.idle(timeout=60)
    assert mh.tg.ephemeral_to(OWNER) == []


async def test_caption_translated(mh: Harness) -> None:
    photo = {"photo": [{"file_id": "p1", "file_unique_id": "pu1", "width": 64, "height": 64}]}
    mh.tg.files["p1"] = blank_image(fmt="PNG", transparent=False)
    await mh.send(group_message(caption="Look at this amazing view from the mountain", sender=SPANISH_SPEAKER,
                                extra=photo))
    await mh.idle(timeout=60)
    sent = mh.tg.ephemeral_to(OWNER)
    assert len(sent) == 1 and "[es] Look at this amazing view" in sent[0].params["text"]


# --- inline -------------------------------------------------------------------------------------


def inline_update(uid: int, query: str, qid: str = "q1") -> Update:
    return Update.model_validate({"update_id": int(time.time() * 1000) % 10**9, "inline_query": {
        "id": qid, "from": {"id": uid, "is_bot": False, "first_name": "U"}, "query": query, "offset": ""}})


def answers(tg: FakeTelegram) -> list[dict[str, Any]]:
    return [c.params for c in tg.calls if c.method == "AnswerInlineQuery"]


async def test_inline_translates_to_english(mh: Harness) -> None:
    await mh.app.storage.upsert_user(OWNER, inline_lang=None)
    await mh.dp.feed_update(mh.app.bot, inline_update(OWNER, "Que locura jajaja 😂"))
    [a] = answers(mh.tg)
    first = a["results"][0]
    assert first["title"] == "That's crazy hahaha 😂"
    assert first["input_message_content"]["message_text"] == "That's crazy hahaha 😂"  # sends the real text
    assert "→ English" in first["description"] and a["is_personal"] is True
    assert "parse_mode" not in first["input_message_content"]  # plain text: no markdown breakage


async def test_inline_already_english_and_empty(mh: Harness) -> None:
    await mh.dp.feed_update(mh.app.bot, inline_update(OWNER, "Are you coming tonight?", "q2"))
    await mh.dp.feed_update(mh.app.bot, inline_update(OWNER, "   ", "q3"))
    a2, a3 = answers(mh.tg)
    assert a2["results"][0]["title"].startswith("Already in English")
    assert a3["results"] == [] and "button" in a3
    assert mh.gemini.jobs == []


async def test_inline_denied_for_unapproved(mh: Harness) -> None:
    await mh.dp.feed_update(mh.app.bot, inline_update(OUTSIDER, "Hola amigos"))
    [a] = answers(mh.tg)
    assert a["results"] == [] and "Private" in a["button"]["text"] and mh.gemini.jobs == []


async def test_inline_debounce_only_latest_query_translated(mh: Harness) -> None:
    import asyncio

    await asyncio.gather(*(mh.dp.feed_update(mh.app.bot, inline_update(OWNER, q, f"id{i}"))
                           for i, q in enumerate(["Que", "Que loc", "Que locura jajaja 😂"])))
    assert len(mh.gemini.jobs) == 1  # superseded keystrokes never reach the provider
    assert len(answers(mh.tg)) == 1


async def test_inline_provider_failure_no_stacktrace(mh: Harness) -> None:
    from translator_bot.providers.base import ErrorKind

    mh.gemini.fail = [ErrorKind.TRANSIENT, ErrorKind.TRANSIENT]
    await mh.dp.feed_update(mh.app.bot, inline_update(OWNER, "Ich komme morgen etwas später", "z"))
    [a] = answers(mh.tg)
    assert a["results"] == [] and "unavailable" in a["button"]["text"]


@pytest.mark.media
async def test_caption_edit_keeps_image_text_and_reuses_the_reading(mh: Harness) -> None:
    import time as _time

    photo = {"photo": [{"file_id": "p9", "file_unique_id": "pu9", "width": 512, "height": 512}]}
    mh.tg.files["p9"] = text_image("NO WAY BRO")
    msg = group_message(caption="Look at this sign", sender=SPANISH_SPEAKER, message_id=4242, extra=photo)
    await mh.send(msg)
    await mh.idle(timeout=60)
    first = mh.tg.ephemeral_to(OWNER)
    assert len(first) == 1 and "¡NI DE BROMA!" in first[0].params["text"]
    calls_before = len(mh.vision.calls)  # type: ignore[attr-defined]
    edited = dict(msg, caption="Look at this funny sign", edit_date=int(_time.time()))
    await mh.send(edited, edited=True)
    await mh.idle(timeout=60)
    edits = mh.tg.ephemeral_edits_to(OWNER)
    assert len(edits) == 1
    text = edits[0].params["text"]
    assert "funny sign" in text and "¡NI DE BROMA!" in text  # caption updated, image text kept
    assert len(mh.vision.calls) == calls_before  # type: ignore[attr-defined]  # no second vision call
    assert len(mh.tg.ephemeral_to(OWNER)) == 1  # edited in place, not a new message
