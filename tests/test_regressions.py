"""Regression tests for issues found in review (MOCKED Telegram/providers)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from aiogram import Dispatcher
from aiogram.types import Update

from tests.conftest import (
    BOT_ID,
    GROUP_ID,
    HINDI,
    OWNER,
    SPANISH_SPEAKER,
    FakeProvider,
    FakeSession,
    FakeTelegram,
    group_message,
)
from tests.fixtures import text_image
from tests.test_e2e_group import Harness, make_settings
from tests.test_media import FakeVision
from translator_bot.app import App
from translator_bot.handlers import build_routers


async def build(tmp_path: Path, detector: Any, tg: FakeTelegram, **over: Any) -> Harness:
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, **over), session=FakeSession(tg), providers={"gemini": gemini},
              detector=detector, vision=FakeVision())
    await app.start()
    dp = Dispatcher()
    for r in build_routers(app):
        dp.include_router(r)
    return Harness(app, dp, tg, gemini, gemini)


async def test_dm_fallback_never_reaches_removed_member(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, HINDI, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    h = await build(tmp_path, detector, tg)
    try:
        await h.app.storage.upsert_chat(GROUP_ID, title="G", enabled=True, bot_is_admin=True)
        await h.app.storage.set_approved(HINDI, True, by=OWNER)
        await h.app.storage.upsert_user(HINDI, lang="hi", dm_fallback=True, started=True)
        await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
        await h.idle()  # membership of HINDI now cached as "member"
        tg.members[GROUP_ID].discard(HINDI)  # kicked, but the cache still says member
        tg.fail_ephemeral_for.add(HINDI)
        await h.send(group_message("Otra cosa importante aquí", sender=SPANISH_SPEAKER))
        await h.idle()
        assert h.tg.dms_to(HINDI) == []  # uncached re-check before DM blocks the leak
        assert h.app.metrics.counters["delivery_fail.dm.not_member"] == 1
    finally:
        await h.app.stop()


async def test_chat_member_update_revokes_immediately(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    h = await build(tmp_path, detector, tg)
    try:
        await h.app.storage.upsert_chat(GROUP_ID, title="G", enabled=True, bot_is_admin=True)
        await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
        await h.idle()
        assert len(tg.ephemeral_to(OWNER)) == 1
        now = int(time.time())
        await h.dp.feed_update(h.app.bot, Update.model_validate({"update_id": 99, "chat_member": {
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "G"}, "from": {"id": 1, "is_bot": False,
                                                                                    "first_name": "a"},
            "date": now, "old_chat_member": {"status": "member", "user": {"id": OWNER, "is_bot": False,
                                                                           "first_name": "o"}},
            "new_chat_member": {"status": "left", "user": {"id": OWNER, "is_bot": False, "first_name": "o"}}}}))
        await h.send(group_message("Otra cosa importante aquí", sender=SPANISH_SPEAKER))
        await h.idle()
        assert len(tg.ephemeral_to(OWNER)) == 1  # left: no further translations even though API says member
    finally:
        await h.app.stop()


async def test_unknown_chat_starts_enabled_by_default(tmp_path: Path, detector: Any) -> None:
    """ENABLE_NEW_GROUPS=all (default): a group works as soon as anyone adds the bot."""
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    h = await build(tmp_path, detector, tg)
    try:
        await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
        await h.idle()
        chat = await h.app.storage.get_chat(GROUP_ID)
        assert chat is not None and chat.enabled  # (delivery waits until the bot's admin status is verified)
    finally:
        await h.app.stop()


async def test_unknown_chat_starts_disabled_when_owner_added_only(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    h = await build(tmp_path, detector, tg, ENABLE_NEW_GROUPS="owner_added")
    try:
        await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
        await h.idle()
        chat = await h.app.storage.get_chat(GROUP_ID)
        assert chat is not None and not chat.enabled
        assert tg.ephemeral_to(OWNER) == [] and h.gemini.jobs == []
    finally:
        await h.app.stop()


async def test_not_admin_skips_without_spending_quota(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}})
    h = await build(tmp_path, detector, tg)
    try:
        await h.app.storage.upsert_chat(GROUP_ID, title="G", enabled=True, bot_is_admin=False)
        await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
        await h.idle()
        assert h.gemini.jobs == [] and h.app.metrics.counters["skip.not_admin"] == 1
    finally:
        await h.app.stop()


async def test_photo_ocr_never_persisted(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    h = await build(tmp_path, detector, tg, MEDIA_ENABLED=True, MEDIA_WORKERS=1)
    try:
        await h.app.storage.upsert_chat(GROUP_ID, title="G", enabled=True, bot_is_admin=True)
        await h.app.storage.upsert_user(OWNER, lang="es")
        tg.files["ph"] = text_image("PRIVATE NOTE")
        photo = {"photo": [{"file_id": "ph", "file_unique_id": "phu", "width": 512, "height": 512}]}
        await h.send(group_message(sender=SPANISH_SPEAKER, extra=photo))
        await h.idle(timeout=60)
        assert len(tg.ephemeral_to(OWNER)) == 1
        cur = await h.app.storage.db.execute("SELECT COUNT(*) FROM media_cache")
        row = await cur.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await h.app.stop()


async def test_plain_text_sent_to_provider_without_html_escapes(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    h = await build(tmp_path, detector, tg)
    try:
        await h.app.storage.upsert_chat(GROUP_ID, title="G", enabled=True, bot_is_admin=True)
        await h.send(group_message("Tom & Jerry < Bugs, ¿verdad amigos?", sender=SPANISH_SPEAKER))
        await h.idle()
        job = h.gemini.jobs[0][0]
        assert job.text == "Tom & Jerry < Bugs, ¿verdad amigos?" and not job.html
        assert "&amp;" in tg.ephemeral_to(OWNER)[0].params["text"]  # but output is escaped for Telegram HTML
    finally:
        await h.app.stop()
