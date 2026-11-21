"""Regression tests for bugs found in the code review of 2026-11-21 (all offline / mocked)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import FakeProvider
from tests.test_providers import make_gemini
from translator_bot.delivery import Deliverer, Delivery, DeliveryError
from translator_bot.detect import Detection, Verdict
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator
from translator_bot.providers.base import ErrorKind, ProviderError, TranslationJob
from translator_bot.resilience import CircuitBreaker
from translator_bot.storage import Storage
from translator_bot.workqueue import Lane

# --- queue workers and request coalescing --------------------------------------------------------


async def test_lane_worker_survives_a_stray_cancelled_error() -> None:
    lane = Lane("t", workers=1, max_total=10, per_chat_max=10, job_timeout=5, metrics=Metrics())
    lane.start()
    done = asyncio.Event()

    async def leaks_cancel() -> None:
        raise asyncio.CancelledError  # e.g. inherited from a coalesced request whose owner timed out

    async def ok() -> None:
        done.set()

    await lane.submit(1, leaks_cancel)
    await lane.submit(1, ok)
    await asyncio.wait_for(done.wait(), 2)  # the worker is still alive and served the next job
    assert not any(t.done() for t in lane._tasks)
    await lane.stop(drain_timeout=1)


async def test_coalesced_waiter_redoes_work_when_owner_is_cancelled() -> None:
    g = FakeProvider("gemini", latency=0.3)
    o = Orchestrator({"gemini": g}, Metrics())
    job = TranslationJob("no manches wey", "en", "es")
    det = Detection("es", 0.9, Verdict.OTHER, informal=True)
    owner = asyncio.create_task(o.translate(job, det))
    await asyncio.sleep(0.05)
    waiter = asyncio.create_task(o.translate(job, det))
    await asyncio.sleep(0.05)
    owner.cancel()  # its queue job timed out
    res = await asyncio.wait_for(waiter, 3)  # used to raise CancelledError and kill the waiter's worker
    assert res is not None and res.needs_translation


# --- circuit breaker ------------------------------------------------------------------------------


def test_half_open_trial_without_outcome_does_not_stick_forever() -> None:
    now = [0.0]
    b = CircuitBreaker(threshold=1, cooldown=30, clock=lambda: now[0])
    b.record_failure()
    now[0] = 31
    assert b.allow()  # trial starts; it ends without record_* (e.g. local RPM cap)
    assert not b.allow()  # still one trial at a time
    now[0] = 70
    assert b.allow()  # stale trial expired: a new one may start (used to return False forever)


async def test_content_errors_do_not_open_the_breaker() -> None:
    g = FakeProvider("gemini")
    g.fail = [ErrorKind.BLOCKED] * 6
    o = Orchestrator({"gemini": g}, Metrics())
    for i in range(6):  # six profane messages in a row
        await o.translate(TranslationJob(f"mensaje grosero {i}", "en", "es"), Detection("es", 0.9, Verdict.OTHER))
    assert o.breakers["gemini"].state == "closed"


async def test_gemini_non_json_200_is_a_provider_error() -> None:
    g, _ = make_gemini(lambda r: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ProviderError) as ei:
        await g.translate(TranslationJob("hola", "en"), quality=False, timeout=5)
    assert ei.value.kind is ErrorKind.TRANSIENT


# --- validation -----------------------------------------------------------------------------------


def test_numbers_next_to_cjk_and_native_digits_are_not_missing() -> None:
    from translator_bot.textutil import missing_entities

    assert missing_entities("We need 2 tickets", "チケットが2枚必要です") == []
    assert missing_entities("2 tickets at 10:30", "2张票，10:30") == []
    assert missing_entities("Meet at 10", "10시에 만나") == []
    assert missing_entities("Meeting at 3 pm", "جلسه ساعت ۳ بعدازظهر") == []
    assert missing_entities("page 12", "पृष्ठ १२") == []
    assert missing_entities("We need 2 tickets", "チケットが3枚必要です") == ["2"]  # changed values still caught
    assert missing_entities("page 12", "पृष्ठ १३") == ["12"]


def test_single_syllable_words_are_not_laughter() -> None:
    from translator_bot.textutil import ContentKind, profile_text

    assert profile_text("Ja").kind is ContentKind.TEXT  # German "yes"
    assert profile_text("jaja").kind is ContentKind.LAUGHTER
    assert profile_text("XD").kind is ContentKind.LAUGHTER


def test_simplified_and_traditional_chinese_are_different_targets(detector: Any) -> None:
    from translator_bot.languages import same_language

    assert detector.detect("我们明天下午见面吧", "zh-Hant").verdict is Verdict.OTHER
    assert detector.detect("我們明天下午見面吧", "zh-Hant").verdict is Verdict.TARGET
    assert detector.detect("我們明天下午見面吧", "zh").verdict is Verdict.OTHER
    assert detector.detect("明天下午", "zh-Hant").verdict is Verdict.TARGET  # shared characters only
    assert not same_language("zh", "zh-Hant") and same_language("zh-CN", "zh") and same_language("pt-BR", "pt")


# --- delivery edits -------------------------------------------------------------------------------


class _EditTransport:
    def __init__(self, errors: list[DeliveryError]) -> None:
        self.errors, self.edits, self.sends = errors, [], []

    async def edit_ephemeral(self, chat_id: int, user_id: int, eph_id: int, text: str) -> None:
        self.edits.append(text)
        if self.errors:
            raise self.errors.pop(0)

    async def send_ephemeral(self, *a: Any, **kw: Any) -> int:
        self.sends.append(a)
        return 99


def _deliverer(transport: Any) -> Deliverer:
    d = Deliverer.__new__(Deliverer)
    d.transport, d.metrics = transport, Metrics()

    async def deliver_one(*a: Any, **kw: Any) -> None:
        transport.sends.append(a)

    d._deliver_one = deliver_one  # type: ignore[method-assign]
    return d


@pytest.mark.parametrize("err", [DeliveryError("retry_after", retry_after=60.0), DeliveryError("chat send limit"),
                                 DeliveryError("api:TelegramNetworkError")])
async def test_transient_edit_failure_does_not_send_a_duplicate(err: DeliveryError) -> None:
    t = _EditTransport([err])
    await _deliverer(t)._edit_one(Delivery(1, "g", None, [5], "M", "es", "en", "<b>hi</b>"), _R(), 7, False)
    assert t.sends == []  # the old translation stays; no second ephemeral


async def test_edit_parse_error_retries_as_plain_text() -> None:
    t = _EditTransport([DeliveryError("parse_error")])
    await _deliverer(t)._edit_one(Delivery(1, "g", None, [5], "M", "es", "en", "<b>hi"), _R(), 7, False)
    assert len(t.edits) == 2 and t.sends == []


async def test_edit_of_expired_ephemeral_sends_fresh() -> None:
    t = _EditTransport([DeliveryError("bad_request:message to edit not found", permanent=True)])
    await _deliverer(t)._edit_one(Delivery(1, "g", None, [5], "M", "es", "en", "hi"), _R(), 7, False)
    assert len(t.sends) == 1


class _R:
    user_id = 42
    lang = "en"


# --- storage / config ----------------------------------------------------------------------------


async def test_supergroup_migration_keeps_settings_when_new_row_exists(tmp_path: Path) -> None:
    st = Storage(tmp_path / "m.sqlite3")
    await st.open()
    try:
        await st.upsert_chat(-100, title="Group", type="group", enabled=True)
        await st.upsert_chat(-1001, title=None, type="supergroup", enabled=False)  # created first, disabled
        await st.migrate_chat_id(-100, -1001)
        chat = await st.get_chat(-1001)
        assert chat is not None and chat.enabled
        assert await st.get_chat(-100) is None
    finally:
        await st.close()


def test_config_rejects_values_that_would_fail_late(monkeypatch: pytest.MonkeyPatch) -> None:
    from translator_bot.config import Settings

    base = {"BOT_TOKEN": "123456:" + "A" * 35, "OWNER_IDS": "1", "GEMINI_API_KEY": "g"}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("EDIT_WINDOW_S", str(7 * 86400))
    with pytest.raises(ValueError, match="EDIT_WINDOW_S"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.delenv("EDIT_WINDOW_S")
    monkeypatch.setenv("BOT_TOKEN", "abc:" + "A" * 35)
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_cjk_clock_formats_and_thai_laughter_are_not_missing_numbers() -> None:
    from translator_bot.textutil import missing_entities

    assert missing_entities("We need 2 tickets for 8:30 tonight", "今夜8時30分のチケットを2枚ください") == []
    assert missing_entities("at 17:00", "午後5時に") == []
    assert missing_entities("at 8:30", "8点30分") == []
    assert missing_entities("at 8:30", "8시 30분에") == []
    assert missing_entities("at 8:30", "9時30分") == ["8:30"]  # a changed time is still caught
    assert missing_entities("555 ขำมากเลยอะ", "lol so funny") == []  # Thai laughter is translated
    assert missing_entities("room 555 please", "habitación por favor") == ["555"]  # a real number stays protected


def test_reply_message_id_invalid_means_resend_without_reply() -> None:
    # observed live 2026-11-22: the ephemeral to one recipient failed with this wording instead of being resent
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    from translator_bot.delivery import TelegramTransport

    exc = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"),
                             message="Bad Request: REPLY_MESSAGE_ID_INVALID")
    assert TelegramTransport._map(exc).reason == "reply_not_found"
