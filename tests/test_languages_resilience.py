from __future__ import annotations

import pytest

from translator_bot.languages import LANGUAGES, resolve_language, suggest_languages
from translator_bot.resilience import CircuitBreaker, DailyQuota, KeyedLimiter, SlidingWindowLimiter, backoff_delay


@pytest.mark.parametrize(("q", "code"), [
    ("en", "en"), ("English", "en"), ("english", "en"), ("Hindi", "hi"), ("हिन्दी", "hi"), ("hinglish", "hi"),
    ("es", "es"), ("Español", "es"), ("espanol", "es"), ("Japanese", "ja"), ("日本語", "ja"), ("pt-BR", "pt"),
    ("zh-TW", "zh-Hant"), ("Chinese (Traditional)", "zh-Hant"), ("farsi", "fa"), ("no", "nb"), ("tagalog", "tl"),
])
def test_resolve(q: str, code: str) -> None:
    lang = resolve_language(q)
    assert lang is not None and lang.code == code


@pytest.mark.parametrize("q", ["", "klingon", "xx", "english please"])
def test_unknown_never_guessed(q: str) -> None:
    assert resolve_language(q) is None


def test_suggest() -> None:
    assert any(lg.code == "pt" for lg in suggest_languages("portu"))
    assert any(lg.code == "hi" for lg in suggest_languages("hin"))


def test_registry_sane() -> None:
    assert len(LANGUAGES) >= 75
    assert all(lg.name and lg.native and lg.script for lg in LANGUAGES.values())


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_sliding_window() -> None:
    c = Clock()
    lim = SlidingWindowLimiter(2, 10.0, clock=c)
    assert lim.try_acquire() and lim.try_acquire() and not lim.try_acquire()
    assert lim.wait_time() == 10.0
    c.t = 10.0
    assert lim.try_acquire()


async def test_acquire_gives_up_when_wait_too_long() -> None:
    lim = SlidingWindowLimiter(1, 60.0)
    assert await lim.acquire(0.1)
    assert not await lim.acquire(0.1)


def test_keyed_limiter_isolated() -> None:
    k = KeyedLimiter(1, 60.0)
    assert k.try_acquire("a") and not k.try_acquire("a") and k.try_acquire("b")


def test_circuit_breaker() -> None:
    c = Clock()
    br = CircuitBreaker(threshold=3, window=60, cooldown=30, clock=c)
    for _ in range(3):
        assert br.allow()
        br.record_failure()
    assert br.state == "open" and not br.allow()
    c.t = 31
    assert br.state == "half_open" and br.allow() and not br.allow()  # single trial
    br.record_failure()
    assert br.state == "open"
    c.t = 62
    assert br.allow()
    br.record_success()
    assert br.state == "closed"


def test_breaker_retry_after_override() -> None:
    c = Clock()
    br = CircuitBreaker(clock=c)
    br.record_failure(open_for=5)
    assert not br.allow()
    c.t = 5.1
    assert br.allow()


def test_daily_quota() -> None:
    q = DailyQuota(2)
    assert q.consume() and q.consume() and not q.consume()
    q.day = "2000-01-01"
    assert q.remaining() == 2
    q.exhaust()
    assert q.remaining() == 0


def test_backoff_bounded() -> None:
    assert all(0 <= backoff_delay(a) <= 4.0 for a in range(10))
