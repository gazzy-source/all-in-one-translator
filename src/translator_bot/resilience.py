"""Rate limiting, circuit breaking and backoff — small, dependency-free, asyncio-native."""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class SlidingWindowLimiter:
    """Allows `limit` events per `window` seconds. Non-blocking `try_acquire` plus awaitable `acquire`."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0] >= self.window:
            self._events.popleft()

    def try_acquire(self) -> bool:
        now = self._clock()
        self._prune(now)
        if len(self._events) < self.limit:
            self._events.append(now)
            return True
        return False

    def wait_time(self) -> float:
        now = self._clock()
        self._prune(now)
        if len(self._events) < self.limit:
            return 0.0
        return max(0.0, float(self.window - (now - self._events[0])))

    async def acquire(self, max_wait: float) -> bool:
        """Wait up to max_wait seconds for a slot. Returns False if it would take longer."""
        deadline = self._clock() + max_wait
        while True:
            if self.try_acquire():
                return True
            wait = self.wait_time()
            if self._clock() + wait > deadline:
                return False
            await asyncio.sleep(min(wait, 0.25) + 0.001)


class KeyedLimiter:
    """One SlidingWindowLimiter per key (user, chat), with bounded memory."""

    def __init__(self, limit: int, window: float, max_keys: int = 50_000) -> None:
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._lims: dict[object, SlidingWindowLimiter] = {}

    def get(self, key: object) -> SlidingWindowLimiter:
        lim = self._lims.get(key)
        if lim is None:
            if len(self._lims) >= self.max_keys:
                # drop idle limiters (no events in window)
                for k in [k for k, v in self._lims.items() if v.wait_time() == 0 and not v._events][:1000]:
                    del self._lims[k]
                if len(self._lims) >= self.max_keys:
                    self._lims.pop(next(iter(self._lims)))
            lim = self._lims[key] = SlidingWindowLimiter(self.limit, self.window)
        return lim

    def try_acquire(self, key: object) -> bool:
        return self.get(key).try_acquire()


@dataclass
class DailyQuota:
    """Counts requests (or chars) per UTC day; resets automatically."""

    limit: int
    used: int = 0
    day: str = field(default_factory=_today)

    def _roll(self) -> None:
        d = _today()
        if d != self.day:
            self.day, self.used = d, 0

    def remaining(self) -> int:
        self._roll()
        return max(0, self.limit - self.used)

    def consume(self, n: int = 1) -> bool:
        self._roll()
        if self.used + n > self.limit:
            return False
        self.used += n
        return True

    def exhaust(self) -> None:
        """Provider told us the quota is gone (e.g. 429 RESOURCE_EXHAUSTED per-day) — stop until rollover."""
        self._roll()
        self.used = self.limit


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """CLOSED -> (threshold failures within window) -> OPEN for cooldown -> HALF_OPEN (1 trial) -> CLOSED/OPEN."""

    def __init__(self, threshold: int = 5, window: float = 60.0, cooldown: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.window, self.cooldown = threshold, window, cooldown
        self._clock = clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._trial_inflight = False
        self._trial_started = 0.0
        self.cooldown_override: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        cd = self.cooldown_override or self.cooldown
        if self._clock() - self._opened_at >= cd:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        st = self.state
        if st == "closed":
            return True
        # A trial that ended without recording an outcome (local rate limit, quota, bad request, deadline,
        # cancellation) would otherwise hold the breaker half-open forever: let a new trial start after a while.
        if st == "half_open" and (not self._trial_inflight or self._clock() - self._trial_started > self.cooldown):
            self._trial_inflight = True
            self._trial_started = self._clock()
            return True
        return False

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None
        self._trial_inflight = False
        self.cooldown_override = None

    def record_failure(self, open_for: float | None = None) -> None:
        now = self._clock()
        self._trial_inflight = False
        if open_for is not None:  # e.g. Retry-After from a 429: open immediately for that long
            self._opened_at, self.cooldown_override = now, open_for
            return
        if self._opened_at is not None:  # failed half-open trial
            self._opened_at = now
            return
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window:
            self._failures.popleft()
        if len(self._failures) >= self.threshold:
            self._opened_at = now
            self._failures.clear()


def backoff_delay(attempt: int, base: float = 0.4, cap: float = 4.0) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * (2**attempt)))
