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
                    and p.media_lane.inflight == 0 and not p.bursts._pending:
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
    assert "Spanish → English" in en[0].params["text"]
    assert "क्या किसी को पता है" in hi[0].params["text"]
    assert en[0].params["reply_parameters"]["message_id"] > 0  # reply context to the original
    assert en[0].params["disable_notification"] is True
    # OWNER never chose a language: default notice shown exactly once
    assert "English is selected by default" in en[0].params["text"]

    await h.send(group_message("Are you coming tonight?", sender=OWNER, name="Owner"))
    await h.idle()
    assert len(h.tg.ephemeral_to(OWNER)) == 1  # own message is never translated back
    hi = h.tg.ephemeral_to(HINDI)
    assert len(hi) == 2 and "क्या तुम आज रात आ रहे हो?" in hi[1].params["text"]

    await h.send(group_message("yaar kya scene hai", sender=HINDI, name="Ravi"))
    await h.idle()
    en = h.tg.ephemeral_to(OWNER)
    assert len(en) == 2 and "dude, what's the plan?" in en[1].params["text"]
    assert "English is selected by default" not in en[1].params["text"]  # notice only once
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
    assert len(h.tg.ephemeral_to(OWNER)) == 2


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
    assert len(edits) == 1 and "locura total" in edits[0].params["text"] and "edited" in edits[0].params["text"]
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
        assert len(out) == 2 and all("approved" in c.params["text"] for c in out)
        assert str(OUTSIDER) in out[0].params["text"] and gemini.jobs == []
    finally:
        await app.stop()


async def test_private_start_open_to_everyone_by_default(h: Harness) -> None:
    await h.send(dm(OUTSIDER, "/start"))
    out = h.tg.dms_to(OUTSIDER)
    assert len(out) == 2  # welcome + language picker
    assert "Send me any" in out[0].params["text"] and str(OUTSIDER) in out[0].params["text"]  # approval hint
    assert "@babel_test_bot" not in out[0].params["text"]  # inline is approved-only by default


async def test_private_chat_translates_text_for_anyone(h: Harness) -> None:
    await h.send(dm(OUTSIDER, "¿Alguien sabe si mañana estará abierto? 😂", mid=9))
    await h.idle()
    [out] = h.tg.dms_to(OUTSIDER)
    assert "Does anyone know if it will be open tomorrow? 😂" in out.params["text"]
    assert "Spanish → English" in out.params["text"]
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
    assert "Here:" in outsider and str(OUTSIDER) in outsider and "/pause" not in outsider
    assert "Groups:" in owner and "/pause" in owner and "@babel_test_bot" in owner
    assert len(owner) < 900


async def test_private_start_onboarding_for_approved(h: Harness) -> None:
    dm = {"message_id": 6, "date": int(time.time()), "chat": {"id": OWNER, "type": "private"},
          "from": {"id": OWNER, "is_bot": False, "first_name": "Own"}, "text": "/start",
          "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}
    await h.send(dm)
    out = h.tg.dms_to(OWNER)
    assert len(out) == 2
    assert "English" in out[0].params["text"] and "selected by default" in out[0].params["text"]
    kb = out[1].params["reply_markup"]["inline_keyboard"]
    assert any(b["callback_data"] == "lang:set:hi" for row in kb for b in row)


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
        await hh.idle()
        [(job, _q)] = [j for j in gemini.jobs if "ni modo" in j[0].text]
        assert [c.text for c in job.context] == ["Are you coming tonight?"]
    finally:
        await app.stop()
