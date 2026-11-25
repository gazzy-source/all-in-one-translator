from __future__ import annotations

import pytest

from translator_bot.recent import Recent


def test_history_expires_and_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr("translator_bot.recent.time.monotonic", lambda: now)
    recent = Recent()
    for mid in range(2001):
        recent.put(1, -2, None, mid, "en", "Group", "translation", "source")
    assert len(recent.rows(1, "en")) == 2000
    assert recent.exact(1, -2, 0, "en", "source") is None
    now += 800
    recent.put(1, -2, None, 1, "en", "Group", "edited", "new source")
    now += 101
    assert [e.html for e in recent.rows(1, "en")] == ["edited"]
    assert recent.exact(1, -2, 1, "en", "source") is None
