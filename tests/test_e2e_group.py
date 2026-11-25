"""End-to-end (MOCKED Telegram + MOCKED providers) through the real aiogram Dispatcher, handlers, pipeline,
delivery and PublicOutputGuard. Mirrors the required acceptance scenario:

  * OWNER   — approved recipient using English (default, never onboarded)
  * HINDI   — approved recipient using Hindi
  * OUTSIDER / SPANISH_SPEAKER — unapproved members

Asserted: approved users get their own private (ephemeral) translations, unapproved users get none,
and the group receives ZERO translator-generated messages.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.types import Update

from tests.conftest import (
    BOT_ID,
    BOT_TOKEN,
    GROUP_ID,
    HINDI,
    OUTSIDER,
    OWNER,
    SPANISH_SPEAKER,
    FakeProvider,
    FakeSession,
    FakeTelegram,
    group_message,
    update,
)
from translator_bot.app import App
from translator_bot.config import Settings
from translator_bot.handlers import build_routers


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    env: dict[str, Any] = {
        "BOT_TOKEN": BOT_TOKEN, "OWNER_IDS": str(OWNER), "DEEPL_API_KEY": "test:fx", "GEMINI_API_KEY": "g",
        "DATABASE_PATH": str(tmp_path / "t.sqlite3"), "BURST_WINDOW_MS": 0, "MEDIA_ENABLED": False,
    }
    env.update(over)
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


class Harness:
    def __init__(self, app: App, dp: Dispatcher, tg: FakeTelegram, gemini: FakeProvider, deepl: FakeProvider) -> None:
        self.app, self.dp, self.tg, self.gemini, self.deepl = app, dp, tg, gemini, deepl

    async def send(self, msg: dict[str, Any], *, edited: bool = False) -> None:
        await self.dp.feed_update(self.app.bot, update(msg, edited=edited))

    async def idle(self, timeout: float = 5.0) -> None:
        p = self.app.pipeline
        assert p is not None
        deadline = time.monotonic() + timeout
        await asyncio.sleep(0.01)
        while time.monotonic() < deadline:
            if p.text_lane.size == 0 and p.text_lane.inflight == 0 and p.media_lane.size == 0 \
                    and p.media_lane.inflight == 0 and not p.bursts._pending and not p.bursts._flushing:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("pipeline did not become idle")


@pytest.fixture
async def h(tmp_path: Path, detector: Any) -> Any:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, HINDI, OUTSIDER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    deepl = FakeProvider("deepl", supported={"en", "es", "hi", "de", "fr"})
    app = App(make_settings(tmp_path), session=FakeSession(tg), providers={"gemini": gemini, "deepl": deepl},
              detector=detector)
    await app.start()
    await app.storage.upsert_chat(GROUP_ID, title="Test Group", type="supergroup", enabled=True, bot_is_admin=True)
    await app.storage.set_approved(HINDI, True, by=OWNER)
    await app.storage.upsert_user(HINDI, lang="hi", onboarded=True)
    dp = Dispatcher()
    for r in build_routers(app):
        dp.include_router(r)
    harness = Harness(app, dp, tg, gemini, deepl)
    yield harness
    await app.stop()


async def test_acceptance_three_members(h: Harness) -> None:
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER, name="Maria"))
    await h.idle()
    en = h.tg.ephemeral_to(OWNER)
    hi = h.tg.ephemeral_to(HINDI)
    assert len(en) == 1 and len(hi) == 1
    assert "Does anyone know if it will be open tomorrow? 😂" in en[0].params["text"]
    assert "Maria" not in en[0].params["text"]  # no sender name by default: the reply quote shows who
    assert "→" not in en[0].params["text"] and "Spanish" not in en[0].params["text"]  # translation only
    assert "क्या किसी को पता है" in hi[0].params["text"]
    assert en[0].params["reply_parameters"]["message_id"] > 0  # reply context to the original
    assert en[0].params["disable_notification"] is True
    # OWNER never chose a language: default notice shown exactly once
    assert "/language" in en[0].params["text"] and "Only you see this" not in en[0].params["text"]

    await h.send(group_message("Are you coming tonight?", sender=OWNER, name="Owner"))
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 1  # own message is never translated back
    hi = h.tg.ephemeral_to(HINDI)
    assert len(hi) == 2 and "क्या तुम आज रात आ रहे हो?" in hi[1].params["text"]

    await h.send(group_message("yaar kya scene hai", sender=HINDI, name="Ravi"))
    await h.idle()
    en = h.tg.ephemeral_to(OWNER)
    assert len(en) == 2 and "dude, what's the plan?" in en[1].params["text"]
    assert "/language" not in en[1].params["text"]  # notice only once
    assert len(h.tg.ephemeral_to(HINDI)) == 2  # romanized Hindi is already Hindi for the Hindi reader

    await h.send(group_message("😂😂😂", sender=OUTSIDER))
    await h.send(group_message("https://example.com", sender=OUTSIDER))
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 2 and len(h.tg.ephemeral_to(HINDI)) == 2

    # unapproved members receive nothing; the group itself receives nothing
    assert h.tg.ephemeral_to(OUTSIDER) == [] and h.tg.ephemeral_to(SPANISH_SPEAKER) == []
    assert h.tg.dms_to(OUTSIDER) == [] and h.tg.dms_to(SPANISH_SPEAKER) == []
    assert h.tg.public_group_output() == []
    assert not any(c.method in ("SendChatAction", "SetMessageReaction", "PinChatMessage", "DeleteMessage")
                   for c in h.tg.calls)


async def test_english_message_not_translated_for_english_reader(h: Harness) -> None:
    await h.send(group_message("The meeting has been moved to Thursday.", sender=OUTSIDER))
    await h.idle()
    assert h.tg.ephemeral_to(OWNER) == []
    assert len(h.tg.ephemeral_to(HINDI)) == 1  # but the Hindi reader gets it
    assert h.tg.public_group_output() == []


async def test_language_change_isolated_and_revocation(h: Harness) -> None:
    await h.app.storage.upsert_user(HINDI, lang="de")
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert "That's crazy hahaha" in h.tg.ephemeral_to(OWNER)[0].params["text"]  # owner unaffected
    assert "[de]" in h.tg.ephemeral_to(HINDI)[0].params["text"]
    await h.app.storage.set_approved(HINDI, False, by=OWNER)
    await h.send(group_message("Que locura jajaja 😂 otra vez", sender=SPANISH_SPEAKER))
    await h.idle()
    assert len(h.tg.ephemeral_to(HINDI)) == 1  # revoked: no more translations
    assert h.tg.ephemeral_edits_to(HINDI) == []
    assert len(h.tg.ephemeral_to(OWNER)) == 1
    assert len(h.tg.ephemeral_edits_to(OWNER)) == 1  # same sender extends owner's bubble


async def test_pause_and_mute(h: Harness) -> None:
    await h.app.storage.upsert_user(HINDI, paused=True)
    await h.app.storage.set_muted(OWNER, GROUP_ID, True)
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert h.tg.ephemeral_to(HINDI) == [] and h.tg.ephemeral_to(OWNER) == []
    assert h.gemini.jobs == [] and h.deepl.jobs == []  # no recipients -> no provider call at all


async def test_ephemeral_failure_isolated_and_never_public(h: Harness) -> None:
    h.tg.fail_ephemeral_for.add(HINDI)
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 1  # other recipient unaffected
    assert h.tg.dms_to(HINDI) == []  # no consent -> no DM fallback
    assert h.tg.public_group_output() == []
    assert h.app.metrics.counters["delivery_fail.ephemeral.ephemeral_not_allowed"] == 1
    digest = h.app.deliverer.digest.drain()
    assert digest and "Test Group" in digest


async def test_dm_fallback_only_with_consent(h: Harness) -> None:
    h.tg.fail_ephemeral_for.add(HINDI)
    await h.app.storage.upsert_user(HINDI, dm_fallback=True, started=True)
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    dms = h.tg.dms_to(HINDI)
    assert len(dms) == 1 and "From Test Group" in dms[0].params["text"]
    assert "https://t.me/c/1234567890/" in dms[0].params["text"]
    assert h.tg.public_group_output() == []


async def test_duplicate_update_translated_once(h: Harness) -> None:
    msg = group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER, message_id=555)
    await h.send(msg)
    await h.send(msg)
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 1


async def test_edit_updates_existing_ephemeral(h: Harness) -> None:
    msg = group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER, message_id=777)
    await h.send(msg)
    await h.idle()
    edited = dict(msg, text="Que locura total jajaja 😂", edit_date=int(time.time()))
    await h.send(edited, edited=True)
    await h.idle()
    edits = h.tg.ephemeral_edits_to(OWNER)
    assert len(edits) == 1 and "locura total" in edits[0].params["text"] and "✏️" in edits[0].params["text"]
    assert len(h.tg.ephemeral_to(OWNER)) == 1  # edited in place, not a second message
    # an edit that doesn't change text (e.g. link preview) does nothing
    await h.send(edited, edited=True)
    await h.idle()
    assert len(h.tg.ephemeral_edits_to(OWNER)) == 1


async def test_bots_own_inline_results_and_commands_skipped(h: Harness) -> None:
    await h.send(group_message("Hola amigos, ¿qué tal?", sender=SPANISH_SPEAKER, via_bot=BOT_ID))
    await h.send(group_message("Hola amigos, ¿qué tal?", sender=999, is_bot=True))
    await h.send(group_message("/start", sender=SPANISH_SPEAKER))
    await h.idle()
    assert h.tg.ephemeral_to(OWNER) == [] and h.gemini.jobs == [] and h.deepl.jobs == []


async def test_hidden_owner_command_answered_only_to_owner(h: Harness) -> None:
    target = group_message("hola", sender=OUTSIDER, name="Pat")
    cmd = group_message("/approve", sender=OWNER, reply_to=target, extra={"ephemeral_message_id": 42})
    await h.send(cmd)
    await h.idle()
    u = await h.app.storage.get_user(OUTSIDER)
    assert u is not None and u.approved
    replies = h.tg.ephemeral_to(OWNER)
    assert len(replies) == 1 and "approved" in replies[0].params["text"]
    assert replies[0].params["reply_parameters"]["ephemeral_message_id"] == 42
    # same command from a non-owner: silently ignored, no output at all
    before = len(h.tg.calls)
    await h.send(group_message("/approve", sender=SPANISH_SPEAKER, reply_to=target))
    await h.idle()
    assert [c for c in h.tg.calls[before:] if c.method.startswith("Send")] == []
    assert h.tg.public_group_output() == []


async def test_burst_coalescing(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, BURST_WINDOW_MS=150), session=FakeSession(tg), providers={"gemini": gemini},
              detector=detector)
    await app.start()
    await app.storage.upsert_chat(GROUP_ID, title="G", enabled=True, bot_is_admin=True)
    dp = Dispatcher()
    for r in build_routers(app):
        dp.include_router(r)
    try:
        for t in ("hola", "¿cómo están todos?", "¿qué hacemos hoy?"):
            await dp.feed_update(app.bot, update(group_message(t, sender=SPANISH_SPEAKER)))
        await asyncio.sleep(0.4)
        await Harness(app, dp, tg, gemini, gemini).idle()
        sends = tg.ephemeral_to(OWNER)
        assert len(sends) == 1  # three rapid messages -> one private translation
        assert len(gemini.jobs) == 1 and gemini.jobs[0][0].text.count("\n") == 2
    finally:
        await app.stop()


def dm(uid: int, text: str | None = None, mid: int = 5, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"message_id": mid, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                         "from": {"id": uid, "is_bot": False, "first_name": "Pat"}, **extra}
    if text is not None:
        d["text"] = text
        if text.startswith("/"):
            d["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return d


async def test_private_start_access_denied_when_private_access_approved(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, OUTSIDER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, PRIVATE_ACCESS="approved"), session=FakeSession(tg),
              providers={"gemini": gemini}, detector=detector)
    await app.start()
    try:
        dp = Dispatcher()
        for r in build_routers(app):
            dp.include_router(r)
        hh = Harness(app, dp, tg, gemini, gemini)
        await hh.send(dm(OUTSIDER, "/start"))
        await hh.send(dm(OUTSIDER, "Hola amigos, ¿qué tal?", mid=6))
        await hh.idle()
        out = tg.dms_to(OUTSIDER)
        assert len(out) == 2 and all("approve" in c.params["text"] for c in out)
        assert str(OUTSIDER) in out[0].params["text"] and gemini.jobs == []
    finally:
        await app.stop()


async def test_private_start_open_to_everyone_by_default(h: Harness) -> None:
    await h.send(dm(OUTSIDER, "/start"))
    out = h.tg.dms_to(OUTSIDER)
    assert len(out) == 1
    assert out[0].params["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lang:menu"
    assert "send or forward" in out[0].params["text"] and "group admin" in out[0].params["text"]  # approval hint
    assert "@babel_test_bot" not in out[0].params["text"]  # inline is approved-only by default


async def test_private_chat_translates_text_for_anyone(h: Harness) -> None:
    await h.send(dm(OUTSIDER, "¿Alguien sabe si mañana estará abierto? 😂", mid=9))
    await h.idle()
    [out] = h.tg.dms_to(OUTSIDER)
    assert "Does anyone know if it will be open tomorrow? 😂" in out.params["text"]
    assert "→" not in out.params["text"]  # only the translation
    assert out.params["reply_parameters"]["message_id"] == 9
    assert h.tg.public_group_output() == []


async def test_private_chat_language_names_and_same_language(h: Harness) -> None:
    await h.send(dm(OUTSIDER, "Hindi"))  # a language name: offer to switch, don't translate
    await h.send(dm(OUTSIDER, "hi", mid=6))  # a bare code outside the prompt is a word
    await h.idle()
    a, b = h.tg.dms_to(OUTSIDER)
    assert "Hindi" in a.params["text"] and a.params["reply_markup"]["inline_keyboard"][0][0]["callback_data"]         == "lang:set:hi"
    assert "Already in" in b.params["text"]
    await h.send(dm(OUTSIDER, "/language", mid=7))
    await h.send(dm(OUTSIDER, "ja", mid=8))  # with the prompt open, codes resolve
    out = h.tg.dms_to(OUTSIDER)
    assert "Japanese" in out[-1].params["text"]
    assert h.gemini.jobs == [] and h.deepl.jobs == []


async def test_private_daily_limit_for_unapproved(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, OUTSIDER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, PRIVATE_DAILY_LIMIT=1), session=FakeSession(tg),
              providers={"gemini": gemini}, detector=detector)
    await app.start()
    try:
        dp = Dispatcher()
        for r in build_routers(app):
            dp.include_router(r)
        hh = Harness(app, dp, tg, gemini, gemini)
        for i, (uid, text) in enumerate([(OUTSIDER, "Hola amigos, ¿qué tal?"), (OUTSIDER, "¿Dónde estás ahora?"),
                                         (OWNER, "¿Dónde estás ahora?"), (OWNER, "Hola amigos, ¿qué tal?")]):
            await hh.send(dm(uid, text, mid=10 + i))
        await hh.idle()
        o = [c.params["text"] for c in tg.dms_to(OUTSIDER)]
        assert len(o) == 2 and sum("[en]" in t for t in o) == 1 and sum("limit" in t for t in o) == 1
        assert all("limit" not in c.params["text"] for c in tg.dms_to(OWNER))  # owners/approved: no daily cap
    finally:
        await app.stop()


async def test_help_is_short_and_role_aware(h: Harness) -> None:
    await h.send(dm(OUTSIDER, "/help"))
    await h.send(dm(OWNER, "/help", mid=6))
    outsider, owner = h.tg.dms_to(OUTSIDER)[0].params["text"], h.tg.dms_to(OWNER)[0].params["text"]
    assert "group admin" in outsider and "/status" not in outsider
    assert "Choose a guide" in owner and "/status" in owner and len(owner) < 250
    kb = h.tg.dms_to(OWNER)[0].params["reply_markup"]["inline_keyboard"]
    assert {b["callback_data"] for row in kb for b in row} == {"lang:menu", "me:show", "me:mygroups", "me:privacy", "me:stop",
                                                                      "help:read", "help:write", "help:setup", "help:missing", "home:open"}
    okb = h.tg.dms_to(OUTSIDER)[0].params["reply_markup"]["inline_keyboard"]
    assert "me:mygroups" not in {b["callback_data"] for row in okb for b in row}  # not approved: no groups


async def test_private_start_onboarding_for_approved(h: Harness) -> None:
    dm = {"message_id": 6, "date": int(time.time()), "chat": {"id": OWNER, "type": "private"},
          "from": {"id": OWNER, "is_bot": False, "first_name": "Own"}, "text": "/start",
          "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}
    await h.send(dm)
    out = h.tg.dms_to(OWNER)
    assert len(out) == 1 and "English" in out[0].params["text"]
    kb = out[0].params["reply_markup"]["inline_keyboard"]
    assert {b["callback_data"] for row in kb for b in row} == {"lang:menu", "recent:menu:0", "me:show", "help:home"}


async def test_show_sender_is_opt_in(h: Harness) -> None:
    chat = await h.app.storage.get_chat(GROUP_ID)
    assert chat is not None
    await h.app.storage.upsert_chat(GROUP_ID, settings={**chat.settings, "show_sender": True})
    h.app.pipeline.invalidate_chat(GROUP_ID)  # type: ignore[union-attr]
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER, name="Maria"))
    await h.idle()
    assert "Maria" in h.tg.ephemeral_to(OWNER)[0].params["text"]


async def test_model_cannot_inject_links_into_plain_translation(h: Harness) -> None:
    src = "¿Puedes revisar tu cuenta del banco hoy por favor?"
    h.gemini.overrides[src] = 'Please check <a href="https://evil.example">your bank</a> today <b>now</b>'
    h.deepl.overrides[src] = h.gemini.overrides[src]
    await h.send(group_message(src, sender=SPANISH_SPEAKER, name="Maria"))
    await h.idle()
    en = h.tg.ephemeral_to(OWNER)
    assert len(en) == 1
    text = en[0].params["text"]
    assert '<a href="https://evil.example">' not in text  # rendered as literal text, never a live link
    assert "&lt;a href=" in text and "&lt;b&gt;now" in text


async def test_edits_respect_disabled_topics_and_send_date(h: Harness) -> None:
    await h.app.storage.set_topic_enabled(GROUP_ID, 42, False)
    topic = {"message_thread_id": 42, "is_topic_message": True}
    msg = group_message("Hola amigos, ¿qué tal todo?", sender=SPANISH_SPEAKER, message_id=901, extra=topic)
    await h.send(dict(msg, edit_date=int(time.time())), edited=True)  # edit in a disabled topic
    old = group_message("Hola amigos, ¿qué tal todo?", sender=SPANISH_SPEAKER, message_id=902)
    old["date"] = int(time.time()) - 3 * 24 * 3600  # sent 3 days ago (> 48 h window), edited now
    await h.send(dict(old, edit_date=int(time.time())), edited=True)
    await h.idle()
    assert h.tg.ephemeral_to(OWNER) == [] and h.gemini.jobs == [] and h.deepl.jobs == []


async def test_health_reports_dead_worker(h: Harness) -> None:
    ok = await h.app.health()
    assert ok["ok"] is True and ok["problems"] == []
    assert h.app.pipeline is not None
    h.app.pipeline.text_lane._tasks[0].cancel()  # simulate a worker that died
    await asyncio.sleep(0.05)
    bad = await h.app.health()
    assert bad["ok"] is False and any("text lane" in p for p in bad["problems"])  # type: ignore[union-attr]


async def test_watchdog_requests_restart_when_unhealthy(h: Harness) -> None:
    assert h.app.pipeline is not None
    for t in h.app.pipeline.text_lane._tasks:
        t.cancel()  # all text workers dead
    await asyncio.sleep(0.05)
    await asyncio.wait_for(h.app._watchdog(interval=0.01), 2)  # two failed checks -> fatal
    assert h.app.fatal.is_set() and "text lane" in (h.app.fatal_reason or "")


async def test_context_is_what_was_said_before_the_message(tmp_path: Path, detector: Any) -> None:
    """Review finding: context was read when the job ran, so a burst's own lines (and later messages) were sent
    as "context" too - the model could merge or re-translate them, and cache keys missed."""
    tg = FakeTelegram(members={GROUP_ID: {OWNER, OUTSIDER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, BURST_WINDOW_MS=300), session=FakeSession(tg),
              providers={"gemini": gemini}, detector=detector)
    await app.start()
    try:
        await app.storage.upsert_chat(GROUP_ID, title="Test Group", type="supergroup", enabled=True,
                                      bot_is_admin=True)
        dp = Dispatcher()
        for r in build_routers(app):
            dp.include_router(r)
        hh = Harness(app, dp, tg, gemini, gemini)
        await hh.send(group_message("Are you coming tonight?", sender=OUTSIDER))
        await hh.send(group_message("no sé todavía", sender=SPANISH_SPEAKER))
        await hh.send(group_message("ni modo jaja", sender=SPANISH_SPEAKER))  # same burst
        # The pending burst still needs its original context after the live buffer turns over.
        for i in range(12):
            app.context.add(GROUP_ID, None, OUTSIDER, f"Later unrelated line {i}")
        assert app.pipeline
        await app.pipeline.bursts.flush_all()
        await hh.idle()
        [(job, _q)] = [j for j in gemini.jobs if "ni modo" in j[0].text]
        assert [c.text for c in job.context] == ["Are you coming tonight?"]
    finally:
        await app.stop()


async def _bg_idle(h: Harness) -> None:
    p = h.app.pipeline
    assert p is not None
    for _ in range(200):
        pending = [t for t in p._bg if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


async def test_approved_member_gets_their_own_language_automatically(h: Harness) -> None:
    """Approve and done: a member who never opened the bot is translated into the language they write in."""
    await h.app.storage.set_approved(SPANISH_SPEAKER, True, by=OWNER)
    for t in ("¿Dónde están todos ahora mismo?", "Yo llego en diez minutos, esperen"):
        await h.send(group_message(t, sender=SPANISH_SPEAKER))
        await h.idle()
        await _bg_idle(h)
    u = await h.app.storage.get_user(SPANISH_SPEAKER)
    assert u is not None and u.lang is None and u.lang_guess == "es"
    await h.send(group_message("Are you guys coming to the party tonight?", sender=OWNER))
    await h.idle()
    got = h.tg.ephemeral_to(SPANISH_SPEAKER)
    assert len(got) == 1 and "[es] Are you guys coming" in got[0].params["text"]


async def test_telegram_app_language_is_used_until_they_write(h: Harness) -> None:
    await h.app.storage.set_approved(OUTSIDER, True, by=OWNER)
    m = group_message("👍", sender=OUTSIDER)
    m["from"]["language_code"] = "de"
    await h.send(m)
    await h.idle()
    await _bg_idle(h)
    await h.send(group_message("¿Alguien sabe si mañana estará abierto?", sender=SPANISH_SPEAKER))
    await h.idle()
    got = h.tg.ephemeral_to(OUTSIDER)
    assert len(got) == 1 and got[0].params["text"].startswith("[de]")


async def test_explicit_language_choice_beats_automatic(h: Harness) -> None:
    await h.app.storage.set_approved(SPANISH_SPEAKER, True, by=OWNER)
    await h.app.storage.upsert_user(SPANISH_SPEAKER, lang="fr", lang_guess="es", tg_lang="es")
    await h.send(group_message("Are you guys coming to the party tonight?", sender=OWNER))
    await h.idle()
    assert h.tg.ephemeral_to(SPANISH_SPEAKER)[0].params["text"].startswith("[fr]")



def _cmd(text: str, sender: int, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
    m = group_message(text, sender=sender, reply_to=reply_to)
    m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return m


async def test_group_admin_approves_members_for_their_group(tmp_path: Path, detector: Any) -> None:
    """The group's owner/admins approve members themselves (no bot owner involved); valid in that group only."""
    ADMIN = 777
    tg = FakeTelegram(members={GROUP_ID: {OWNER, ADMIN, SPANISH_SPEAKER, OUTSIDER, BOT_ID}}, admins={GROUP_ID})
    real = tg.respond

    def respond(method: Any) -> Any:  # ADMIN owns the group (creator)
        if type(method).__name__ == "GetChatMember" and method.user_id == ADMIN:
            return {"status": "creator", "user": {"id": ADMIN, "is_bot": False, "first_name": "Adm"},
                    "is_anonymous": False}
        return real(method)

    tg.respond = respond  # type: ignore[method-assign]
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path), session=FakeSession(tg), providers={"gemini": gemini}, detector=detector)
    await app.start()
    try:
        await app.storage.upsert_chat(GROUP_ID, title="G", type="supergroup", enabled=True, bot_is_admin=True)
        dp = Dispatcher()
        for r in build_routers(app):
            dp.include_router(r)
        hh = Harness(app, dp, tg, gemini, gemini)
        target = group_message("hola", sender=OUTSIDER)
        await hh.send(_cmd("/approve", ADMIN, reply_to=target))
        assert await app.resolver.is_approved_in(GROUP_ID, OUTSIDER)
        assert not await app.resolver.is_approved_in(-100999, OUTSIDER)  # this group only
        u = await app.storage.get_user(OUTSIDER)
        assert u is not None and not u.approved  # not a global (bot-owner) approval
        # group-admin approval never leaks outside the group (private caps, inline, billing stay global)
        assert not await app.resolver.is_approved(OUTSIDER)
        assert await app.resolver.has_group_access(OUTSIDER)
        assert any("for this group" in c.params["text"] for c in tg.ephemeral_to(ADMIN))
        await hh.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
        await hh.idle()
        assert len(tg.ephemeral_to(OUTSIDER)) == 1  # they now get translations here
        # an ordinary member can't approve anyone
        await hh.send(_cmd("/approve", SPANISH_SPEAKER, reply_to=group_message("x", sender=OWNER)))
        assert not await app.storage.is_chat_approved(OWNER, GROUP_ID)
        assert tg.public_group_output() == []
    finally:
        await app.stop()


async def test_group_admin_approveall_translates_for_everyone_there(tmp_path: Path, detector: Any) -> None:
    """No bot owner involved: the group's own admin turns translations on for everyone in it."""
    ADMIN, JOINER = 778, 779
    tg = FakeTelegram(members={GROUP_ID: {OWNER, ADMIN, SPANISH_SPEAKER, OUTSIDER, JOINER, BOT_ID}},
                      admins={GROUP_ID})
    real = tg.respond

    def respond(method: Any) -> Any:
        if type(method).__name__ == "GetChatMember" and method.user_id == ADMIN:
            return {"status": "creator", "user": {"id": ADMIN, "is_bot": False, "first_name": "Adm"},
                    "is_anonymous": False}
        return real(method)

    tg.respond = respond  # type: ignore[method-assign]
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path), session=FakeSession(tg), providers={"gemini": gemini}, detector=detector)
    await app.start()
    try:
        await app.storage.upsert_chat(GROUP_ID, title="G", type="supergroup", enabled=True, bot_is_admin=True)
        dp = Dispatcher()
        for r in build_routers(app):
            dp.include_router(r)
        hh = Harness(app, dp, tg, gemini, gemini)
        await hh.send(group_message("Are you coming tonight?", sender=OUTSIDER))  # OUTSIDER has written here
        await hh.idle()
        await hh.send(_cmd("/approveall", ADMIN))
        assert any("Everyone here" in c.params["text"] for c in tg.ephemeral_to(ADMIN))
        await hh.dp.feed_update(app.bot, Update.model_validate({"update_id": 99_001, "chat_member": {
            "chat": {"id": GROUP_ID, "type": "supergroup", "title": "G"},
            "from": {"id": JOINER, "is_bot": False, "first_name": "J"}, "date": int(time.time()),
            "old_chat_member": {"status": "left", "user": {"id": JOINER, "is_bot": False, "first_name": "J"}},
            "new_chat_member": {"status": "member", "user": {"id": JOINER, "is_bot": False, "first_name": "J"}}}}))
        await hh.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
        await hh.idle()
        assert len(tg.ephemeral_to(OUTSIDER)) == 1 and len(tg.ephemeral_to(JOINER)) == 1  # writer + joiner
        assert await app.resolver.is_approved_in(GROUP_ID, SPANISH_SPEAKER)  # /tr works for everyone here
        assert tg.public_group_output() == []
        await hh.send(_cmd("/revokeall", ADMIN))
        assert not await app.resolver.everyone_mode(GROUP_ID)
    finally:
        await app.stop()



async def test_group_daily_limit_protects_the_shared_quota(tmp_path: Path, detector: Any) -> None:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, SPANISH_SPEAKER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, GROUP_DAILY_LIMIT=1), session=FakeSession(tg), providers={"gemini": gemini},
              detector=detector)
    await app.start()
    try:
        await app.storage.upsert_chat(GROUP_ID, title="G", type="supergroup", enabled=True, bot_is_admin=True)
        dp = Dispatcher()
        for r in build_routers(app):
            dp.include_router(r)
        hh = Harness(app, dp, tg, gemini, gemini)
        await hh.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
        await hh.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
        await hh.idle()
        assert len(tg.ephemeral_to(OWNER)) == 1 and app.metrics.counters.get("skip.group_daily_limit") == 1
    finally:
        await app.stop()


async def test_readers_skip_languages_they_also_read(h: Harness) -> None:
    await h.app.storage.upsert_user(HINDI, also_reads="es")
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert h.tg.ephemeral_to(HINDI) == [] and len(h.tg.ephemeral_to(OWNER)) == 1


async def test_all_readers_understand_source_spends_no_provider_quota(h: Harness) -> None:
    for uid in (OWNER, HINDI):
        await h.app.storage.upsert_user(uid, also_reads="es")
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert not h.gemini.jobs and not h.deepl.jobs
    assert not h.tg.ephemeral_to(OWNER) and not h.tg.ephemeral_to(HINDI)


async def test_additional_reading_does_not_hide_mixed_language_content(h: Harness) -> None:
    await h.app.storage.upsert_user(HINDI, also_reads="es")
    await h.send(group_message("Mañana voy, but I'm not sure about the time", sender=SPANISH_SPEAKER))
    await h.idle()
    assert len(h.tg.ephemeral_to(HINDI)) == 1
    assert h.tg.public_group_output() == []


async def test_revocation_during_provider_call_blocks_delivery(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = h.app.pipeline
    assert pipeline is not None
    translate = pipeline._translate_for

    async def revoke(*args: Any, **kwargs: Any) -> Any:
        result = await translate(*args, **kwargs)
        await h.app.storage.set_approved(HINDI, False, by=OWNER)
        return result

    monkeypatch.setattr(pipeline, "_translate_for", revoke)
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert not h.tg.ephemeral_to(HINDI) and len(h.tg.ephemeral_to(OWNER)) == 1


async def test_reading_picker_callback_saves_and_returns_to_settings(h: Harness) -> None:
    from tests.test_billing import press

    for data in ("also:menu", "also:page:2", "also:tog:ja:2", "me:orig", "also:done"):
        await h.dp.feed_update(h.app.bot, Update.model_validate({"update_id": 987, **press(HINDI, data)}))
    user = await h.app.storage.get_user(HINDI)
    assert user and user.also_reads_set == {"ja"} and user.show_original
    owner = await h.app.storage.get_user(OWNER)
    assert owner is None or (not owner.also_reads_set and not owner.show_original)
    assert h.tg.public_group_output() == []


async def test_personal_show_original(h: Harness) -> None:
    await h.app.storage.upsert_user(OWNER, show_original=True)
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    text = h.tg.ephemeral_to(OWNER)[0].params["text"]
    assert text.startswith("<blockquote>¿Alguien sabe") and "Does anyone know" in text
    assert "<blockquote>" not in h.tg.ephemeral_to(HINDI)[0].params["text"]  # only for who asked


async def test_same_sender_run_grows_one_bubble(h: Harness) -> None:
    """Less noise: consecutive messages of one sender extend the reader's last translation in place."""
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 1  # still one bubble
    [edit] = h.tg.ephemeral_edits_to(OWNER)
    assert "Does anyone know" in edit.params["text"] and "That's crazy hahaha" in edit.params["text"]
    # someone else speaks: the next message starts a new bubble
    await h.send(group_message("ok 👍 see you", sender=OUTSIDER))
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 2


async def test_edit_of_appended_original_preserves_other_translation(h: Harness) -> None:
    first = group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER)
    second = group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER)
    await h.send(first)
    await h.idle()
    await h.send(second)
    await h.idle()
    assert len(h.tg.ephemeral_edits_to(OWNER)) == 1
    first["text"] = "Que locura jajaja 😂 otra vez"
    await h.send(first, edited=True)
    await h.idle()
    # The shared bubble still contains both originals; a marked correction is sent separately.
    assert len(h.tg.ephemeral_edits_to(OWNER)) == 1
    assert len(h.tg.ephemeral_to(OWNER)) == 2
    assert "✏️" in h.tg.ephemeral_to(OWNER)[-1].params["text"]
    assert "That's crazy hahaha" in h.tg.ephemeral_edits_to(OWNER)[0].params["text"]


@pytest.mark.parametrize("also_reads,has_caption,has_media", [("es", False, True), ("en", True, False),
                                                             ("es,en", False, False)])
async def test_caption_and_media_reading_preferences_are_independent(
    h: Harness, monkeypatch: pytest.MonkeyPatch, also_reads: str, has_caption: bool, has_media: bool,
) -> None:
    from types import SimpleNamespace

    from translator_bot.media.processor import MediaText

    pipeline = h.app.pipeline
    assert pipeline is not None
    monkeypatch.setattr(pipeline.s, "media_enabled", True)

    async def extract(**kwargs: Any) -> MediaText:
        return MediaText(True, "The meeting has been moved to Thursday.", "en", 1.0,
                         {"hi": "MEDIA_ONLY", "en": ""}, "vision")

    monkeypatch.setattr(pipeline, "media", SimpleNamespace(extract=extract))
    await h.app.storage.upsert_user(HINDI, also_reads=also_reads, show_original=True)
    await h.send(group_message(sender=SPANISH_SPEAKER, caption="¿Alguien sabe si mañana estará abierto? 😂",
                               extra={"photo": [{"file_id": "p", "file_unique_id": "pu", "width": 100,
                                                   "height": 100}]}))
    await h.idle()
    texts = [c.params["text"] for c in h.tg.ephemeral_to(HINDI)]
    assert bool(texts) == (has_caption or has_media)
    if texts:
        assert ("क्या किसी को पता है" in texts[0]) == has_caption
        assert ("MEDIA_ONLY" in texts[0]) == has_media
        assert ("¿Alguien sabe" in texts[0]) == has_caption
        assert ("The meeting has been moved" in texts[0]) == has_media
    assert h.tg.public_group_output() == []


async def test_help_navigation_personal_controls_and_language(h: Harness) -> None:
    from tests.test_billing import press

    async def tap(data: str) -> None:
        await h.dp.feed_update(h.app.bot, Update.model_validate({"update_id": 998, **press(HINDI, data)}))

    for topic in ("read", "write", "setup", "missing"):
        await tap("help:" + topic)
        edit = [c for c in h.tg.calls if c.method == "EditMessageText"][-1]
        assert "help:home" in str(edit.params["reply_markup"])
        await tap("help:home")
    await tap("me:stop")
    await tap("me:stop")  # an old keyboard must not accidentally resume
    await tap("lang:set:my")
    user = await h.app.storage.get_user(HINDI)
    assert user and user.paused and user.lang == "my"
    await h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert not h.tg.ephemeral_to(HINDI) and h.tg.ephemeral_to(OWNER)
    await tap("me:resume")
    await tap("me:resume")
    user = await h.app.storage.get_user(HINDI)
    assert user and not user.paused and user.lang == "my"
    await tap("lang:set:hi")
    await h.send(group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert h.tg.ephemeral_to(HINDI)
    assert not h.tg.public_group_output()


@pytest.mark.parametrize("everyone", [False, True])
async def test_group_approved_reader_can_use_private_controls(h: Harness, everyone: bool) -> None:
    from tests.test_billing import press

    h.app.s.private_access = "approved"
    if everyone:
        await h.app.storage.upsert_chat(GROUP_ID, settings={"everyone": True})
        await h.app.storage.mark_participant(OUTSIDER, GROUP_ID)
    else:
        await h.app.storage.set_chat_approved(OUTSIDER, GROUP_ID, True, OWNER)
    await h.send(dm(OUTSIDER, "/help"))
    assert "me:stop" in str(h.tg.dms_to(OUTSIDER)[-1].params["reply_markup"])
    for data in ("me:stop", "lang:set:my", "help:home"):
        await h.dp.feed_update(h.app.bot, Update.model_validate({"update_id": 999, **press(OUTSIDER, data)}))
    user = await h.app.storage.get_user(OUTSIDER)
    assert user and user.paused and user.lang == "my"
    assert not h.tg.public_group_output()

    assert not await h.app.resolver.is_approved(OUTSIDER)
    await h.send(dm(OUTSIDER, "/language", mid=553))
    await h.send(dm(OUTSIDER, "Burmese", mid=554))
    assert "lang:set:my" in str(h.tg.dms_to(OUTSIDER)[-1].params["reply_markup"])
    await h.send(dm(OUTSIDER, "/language", mid=556))
    await h.send(dm(OUTSIDER, "Please translate this private message", mid=555))
    assert "Ask a group admin" in h.tg.dms_to(OUTSIDER)[-1].params["text"]


@pytest.mark.parametrize(("actor", "role", "allowed"), [
    (OUTSIDER, "creator", True), (OWNER, "creator", True),
    (OUTSIDER, "administrator", False), (OWNER, "administrator", False),
    (OUTSIDER, "member", False), (OWNER, "member", False), (OWNER, "error", False),
])
async def test_approveall_requires_current_group_owner(
        h: Harness, monkeypatch: pytest.MonkeyPatch, actor: int, role: str, allowed: bool) -> None:
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChatMember

    original = h.app.bot.get_chat_member

    async def member(chat_id: int, user_id: int, **kwargs: Any) -> Any:
        if user_id == actor:
            if role == "error":
                raise TelegramBadRequest(method=GetChatMember(chat_id=chat_id, user_id=user_id), message="unavailable")
            return SimpleNamespace(status=role)
        return await original(chat_id, user_id, **kwargs)

    monkeypatch.setattr(h.app.bot, "get_chat_member", member)
    await h.send(_cmd("/approveall", actor))
    chat = await h.app.storage.get_chat(GROUP_ID)
    assert chat and bool(chat.settings.get("everyone")) is allowed
    if allowed:
        await h.app.storage.upsert_chat(GROUP_ID, settings={"everyone": False})
        role = "administrator"  # ownership transferred: never reuse a cached positive result
        await h.send(_cmd("/approveall", actor))
        chat = await h.app.storage.get_chat(GROUP_ID)
        assert chat and not chat.settings.get("everyone")
    assert not h.tg.public_group_output()


async def test_approving_old_message_does_not_restore_departed_membership(h: Harness) -> None:
    h.tg.members[GROUP_ID].remove(OUTSIDER)
    h.app.membership.set(GROUP_ID, OUTSIDER, False)
    await h.send(_cmd("/approve", OWNER, reply_to=group_message("old message", sender=OUTSIDER)))
    assert await h.app.resolver.is_approved(OUTSIDER)
    recipients = await h.app.resolver.resolve(GROUP_ID, SPANISH_SPEAKER)
    assert OUTSIDER not in {r.user_id for r in recipients}
    assert not h.tg.public_group_output()


async def test_admin_menus_are_per_person_and_approveall_only_for_creator(
        h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from translator_bot.handlers import register_owner_group_commands

    async def admins(chat_id: int) -> Any:
        return [SimpleNamespace(status=role, user=SimpleNamespace(id=uid, is_bot=False))
                for uid, role in ((OWNER, "creator"), (HINDI, "administrator"))]

    monkeypatch.setattr(h.app.bot, "get_chat_administrators", admins)
    h.tg.calls.clear()
    await register_owner_group_commands(h.app, GROUP_ID)
    menus = [c.params for c in h.tg.calls if c.method == "SetMyCommands"]
    assert len(menus) == 2
    for menu in menus:
        assert menu["scope"]["type"] == "chat_member"
        commands = {c["command"] for c in menu["commands"]}
        assert ("approveall" in commands) is (menu["scope"]["user_id"] == OWNER)
        assert all(c["is_ephemeral"] for c in menu["commands"])
    assert not h.tg.public_group_output()


@pytest.mark.parametrize("body", ["Are you coming tonight?", "¿Alguien sabe si mañana estará abierto? 😂"])
async def test_reader_gets_translated_parent_and_current_reply(h: Harness, body: str) -> None:
    parent = group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER, message_id=150)
    reply = group_message(body, sender=OUTSIDER, message_id=151)
    reply["reply_to_message"] = parent
    await h.send(reply)
    await h.idle()
    [sent] = h.tg.ephemeral_to(OWNER)
    assert "crazy hahaha" in sent.params["text"].split("</blockquote>")[0]
    assert ("coming tonight" if body.startswith("Are") else "Does anyone know") in sent.params["text"].split("</blockquote>")[1]
    assert sent.params["reply_parameters"]["message_id"] == 151
    assert not h.tg.public_group_output()


async def test_tagged_parent_uses_its_own_reply_context(h: Harness) -> None:
    first = group_message("Which team?", sender=OWNER, message_id=910)
    parent = group_message("El equipo rojo", sender=OUTSIDER, message_id=911)
    parent["reply_to_message"] = first
    child = group_message("Estoy de acuerdo", sender=SPANISH_SPEAKER, message_id=912)
    child["reply_to_message"] = parent
    await h.send(parent)
    await h.idle()
    h.app.orchestrator.cache.clear()
    h.gemini.jobs.clear()
    await h.send(child)
    await h.idle()
    jobs = [j for j, _ in h.gemini.jobs if j.text == "El equipo rojo" and j.target == "en"]
    assert jobs and all(j.reply_to and j.reply_to.text == "Which team?" for j in jobs)
    body = h.tg.ephemeral_to(OWNER)[-1].params["text"]
    assert "<blockquote>" in body and "Replying to" not in body and "<b>Reply:" not in body


async def test_recent_reader_pages_are_scoped_and_recheck_membership(h: Harness) -> None:
    from tests.test_billing import press

    for mid in range(1, 8):
        h.app.deliverer.recent.put(HINDI, GROUP_ID, None, mid, "hi", "<Group>", f"line {mid}", f"source {mid}")
    h.app.deliverer.recent.put(OWNER, GROUP_ID, None, 99, "hi", "Group", "other reader", None)
    h.app.deliverer.recent.put(HINDI, GROUP_ID, 8, 100, "hi", "Group", "other topic", None)

    async def page(anchor: int) -> str:
        payload = press(HINDI, f"recent:p:{GROUP_ID}:0:{anchor}")
        await h.dp.feed_update(h.app.bot, Update.model_validate({"update_id": 997, **payload}))
        return str([c for c in h.tg.calls if c.method == "EditMessageText"][-1].params["text"])

    await h.send(dm(HINDI, "/recent"))
    assert "recent:p:" in str(h.tg.dms_to(HINDI)[-1].params["reply_markup"])
    newest = await page(0)
    assert "line 3" in newest and "line 7" in newest and "line 2" not in newest and "other" not in newest
    assert "&lt;Group&gt;" in newest
    older = await page(2)
    assert older.index("line 1") < older.index("line 2") and "line 3" not in older
    h.tg.members[GROUP_ID].discard(HINDI)
    assert "unavailable" in await page(0)
    assert not h.tg.public_group_output()


async def test_play_argument_reaches_translation_without_command(h: Harness) -> None:
    await h.send(group_message("/play@music_bot ¿Alguien sabe si mañana estará abierto? 😂", sender=SPANISH_SPEAKER))
    await h.idle()
    assert "Does anyone know" in h.tg.ephemeral_to(OWNER)[0].params["text"]
    assert not h.tg.public_group_output()


async def test_recent_exact_reply_skips_queue_and_rejects_changed_text(h: Harness) -> None:
    source = group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER)
    await h.send(source)
    await h.idle()
    mid = source["message_id"]
    cache = h.app.deliverer.recent
    assert cache.exact(OWNER, GROUP_ID, mid, "en", source["text"])
    assert cache.exact(OWNER, GROUP_ID, mid, "en", source["text"] + "!") is None
    assert cache.exact(OUTSIDER, GROUP_ID, mid, "en", source["text"]) is None
    assert cache.exact(OWNER, GROUP_ID, mid, "my", source["text"]) is None
    count = len(h.tg.ephemeral_to(OWNER))
    await h.send(group_message("/tr", sender=OWNER, reply_to=source))
    fresh = h.tg.ephemeral_to(OWNER)[count:]
    assert len(fresh) == 1 and "Translating" not in fresh[0].params["text"]
    assert fresh[0].params["reply_parameters"]["message_id"] == mid
    await h.send(dm(OWNER, "/forgetme"))
    assert not cache.rows(OWNER, "en")


async def test_slow_claim_preserves_consecutive_message_order(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = h.app.pipeline
    assert pipeline
    pipeline.bursts.window = 10
    entered, release = asyncio.Event(), asyncio.Event()
    claim = h.app.storage.claim_message

    async def delayed(chat: int, mid: int, *args: Any) -> bool:
        if mid == 1800:
            entered.set()
            await release.wait()
        return await claim(chat, mid, *args)

    monkeypatch.setattr(h.app.storage, "claim_message", delayed)
    first = asyncio.create_task(h.send(group_message("Que locura jajaja 😂", message_id=1800)))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(h.send(group_message("¿Alguien sabe si mañana estará abierto? 😂", message_id=1801)))
    try:
        await asyncio.wait({second}, timeout=0.15)
    finally:
        release.set()
        await asyncio.gather(first, second)
    await pipeline.bursts.flush_all()
    await h.idle()
    sent = h.tg.ephemeral_to(OWNER)
    assert len(sent) == 1 and sent[0].params["reply_parameters"]["message_id"] == 1801


async def test_reverting_pending_edit_translates_final_text(h: Harness) -> None:
    pipeline = h.app.pipeline
    assert pipeline
    pipeline.bursts.window = 10
    msg = group_message("Que locura jajaja 😂")
    await h.send(msg)
    await h.send({**msg, "text": "¿Alguien sabe si mañana estará abierto? 😂"}, edited=True)
    await h.send(msg, edited=True)
    await pipeline.bursts.flush_all()
    await h.idle()
    assert "crazy hahaha" in h.tg.ephemeral_to(OWNER)[0].params["text"]


async def test_one_off_recovers_quoted_parent_when_telegram_omits_nested_reply(h: Harness) -> None:
    parent = group_message("Que locura jajaja 😂", sender=SPANISH_SPEAKER)
    reply = group_message("¿Alguien sabe si mañana estará abierto? 😂", reply_to=parent)
    await h.send(reply)
    await h.idle()
    h.app.deliverer.recent.forget(OWNER)  # force the on-demand path
    shallow = dict(reply)
    shallow.pop("reply_to_message")
    await h.send(group_message("/tr", sender=OWNER, reply_to=shallow))
    await h.idle()
    body = h.tg.ephemeral_edits_to(OWNER)[-1].params["text"]
    assert "crazy hahaha" in body.split("</blockquote>")[0]
    assert "Does anyone know" in body.split("</blockquote>")[1]
    assert not h.tg.public_group_output()


async def test_simultaneous_bursts_keep_topic_order_and_all_message_ids(h: Harness) -> None:
    pipeline = h.app.pipeline
    assert pipeline
    pipeline.bursts.window = 10
    messages = [group_message(f"Que locura jajaja 😂 {i}", message_id=2000 + i, sender=SPANISH_SPEAKER,
                              extra={"is_topic_message": True, "message_thread_id": 100 + i % 4})
                for i in range(40)]
    await asyncio.gather(*(h.send(msg) for msg in messages))
    await pipeline.bursts.flush_all()
    await h.idle(timeout=15)
    for index in range(40):
        mid = 2000 + index
        rec = pipeline._units[GROUP_ID, mid]
        first = 2000 + index % 4 + 20 * (index // 20)
        assert rec.meta.thread_id == 100 + index % 4
        assert [item.message_id for item in rec.items] == list(range(first, first + 20, 4))
        deliveries = await h.app.storage.deliveries_for(GROUP_ID, mid)
        assert any(row["recipient_id"] == OWNER and row["status"] == "sent" for row in deliveries)
    assert not h.tg.public_group_output()


async def test_jobs_without_readers_leave_processing_state(h: Harness) -> None:
    for reader in (OWNER, HINDI):
        await h.app.storage.upsert_user(reader, paused=True)
    text = group_message("Que locura jajaja 😂")
    caption = group_message(caption="Que locura jajaja 😂", extra={"photo": [
        {"file_id": "p", "file_unique_id": "p1", "width": 10, "height": 10}]})
    for msg in (text, caption):
        await h.send(msg)
    await h.idle()
    for msg in (text, caption):
        row = await h.app.storage.get_message(GROUP_ID, msg["message_id"])
        assert row and row["status"] == "no_recipients"
    assert not h.tg.ephemeral_to(OWNER) and not h.tg.ephemeral_to(HINDI)
