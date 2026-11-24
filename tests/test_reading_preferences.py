"""Reading preferences and consecutive bubbles: upgrades, isolation, and safe fallbacks."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from translator_bot import ui
from translator_bot.delivery import Deliverer, Delivery, DeliveryError
from translator_bot.languages import LANGUAGES
from translator_bot.metrics import Metrics
from translator_bot.recipients import Recipient
from translator_bot.storage import MIGRATIONS, Storage, User


@pytest.mark.parametrize("version", [3, 4])
async def test_upgrade_existing_database_preserves_preferences(tmp_path: Path, version: int) -> None:
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    for sql in MIGRATIONS[:version]:
        db.executescript(sql)
    db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    db.execute("INSERT INTO schema_version VALUES (?)", (version,))
    db.execute("INSERT INTO users (user_id, lang, approved, private_credit, updated_at) VALUES (1,'hi',1,500,0)")
    db.commit()
    db.close()
    st = Storage(path)
    await st.open()
    try:
        u = await st.get_user(1)
        assert u and u.lang == "hi" and u.approved and u.private_credit == 500
        assert not u.also_reads_set and not u.show_original
        await st.upsert_user(1, also_reads="es,zh-Hant", show_original=True)
    finally:
        await st.close()
    await st.open()
    try:
        u = await st.get_user(1)
        assert u and u.also_reads_set == {"es", "zh-Hant"} and u.show_original
        await st.delete_user_data(1)
        u = await st.get_user(1)
        assert u and not u.also_reads_set and not u.show_original and u.private_credit == 500
    finally:
        await st.close()


def test_picker_reaches_every_language_and_keeps_selection() -> None:
    user = User(1, lang="en", also_reads="ja,zh-Hant")
    codes = set()
    for page in range(10):
        for row in ui.also_reads_kb(user, "en", page).inline_keyboard:
            for button in row:
                data = button.callback_data or ""
                if data.startswith("also:tog:"):
                    code = data.split(":")[2]
                    codes.add(code)
                    assert button.text.startswith("✅") == (code in user.also_reads_set)
                    assert len(data.encode()) <= 64
    assert codes == set(LANGUAGES) - {"en"}


def test_additional_reading_preserves_chinese_script_choice() -> None:
    reader = Recipient(1, "en", False, False, False, frozenset({"zh-Hant", "pt"}))
    assert reader.reads("zh-TW") and reader.reads("pt-BR")
    assert not reader.reads("zh") and not reader.reads(None)


class Transport:
    def __init__(self) -> None:
        self.sends: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self.dms: list[str] = []
        self.error: DeliveryError | None = None

    async def send_ephemeral(self, chat: int, user: int, text: str, **kwargs: Any) -> int:
        self.sends.append(text)
        return len(self.sends)

    async def edit_ephemeral(self, chat: int, user: int, ephemeral: int, text: str) -> None:
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        self.edits.append((ephemeral, text))

    async def send_private(self, user: int, text: str) -> int:
        self.dms.append(text)
        return len(self.dms)


@pytest.fixture
async def delivery() -> Any:
    st = Storage(":memory:")
    await st.open()
    t = Transport()
    d = Deliverer(t, st, Metrics())
    r = Recipient(1, "en", False, True, False)
    first = Delivery(-1001, "G", None, [10], "Maria", "es", "en", "First", sender_id=2)
    yield d, t, st, r, first
    await st.close()


async def test_append_records_every_original_and_serializes_updates(delivery: Any) -> None:
    d, t, st, r, first = delivery
    await d.deliver(first, [r])
    await asyncio.gather(
        d.deliver(replace(first, message_ids=[11], translation_html="Second", append_after=10), [r]),
        d.deliver(replace(first, message_ids=[12], translation_html="Third", append_after=11), [r]),
    )
    assert t.sends == ["First"]
    assert t.edits[-1] == (1, "First\nSecond\nThird")
    assert await st.messages_for_ephemeral(-1001, 1, 1) == [10, 11, 12]


@pytest.mark.parametrize("change", [
    {"target_lang": "de"}, {"sender_id": 3}, {"thread_id": 9}, {"append_after": 9},
    {"message_ids": [9]}, {"show_sender": True}, {"edited": True},
    {"translation_html": "x" * 4090},
])
async def test_incompatible_bubble_starts_fresh(delivery: Any, change: dict[str, Any]) -> None:
    d, t, _st, r, first = delivery
    await d.deliver(first, [r])
    second = replace(first, message_ids=[11], translation_html="Second", append_after=10)
    await d.deliver(replace(second, **change), [r])
    assert len(t.sends) == 2 and not t.edits


async def test_preference_change_starts_fresh(delivery: Any) -> None:
    d, t, _st, r, first = delivery
    await d.deliver(first, [r])
    await d.deliver(replace(first, message_ids=[11], original_plain="Original", append_after=10),
                    [replace(r, show_original=True)])
    assert len(t.sends) == 2 and t.sends[-1].startswith("<blockquote>Original")


@pytest.mark.parametrize("reason", ["not_member", "forbidden"])
async def test_append_membership_failure_never_resends_or_dms(delivery: Any, reason: str) -> None:
    d, t, st, r, first = delivery
    await d.deliver(first, [r])
    t.error = DeliveryError(reason, permanent=True)
    await d.deliver(replace(first, message_ids=[11], append_after=10), [r])
    assert len(t.sends) == 1 and not t.dms
    assert (await st.deliveries_for(-1001, 11))[0]["status"] == "failed"


async def test_expired_bubble_sends_fresh(delivery: Any) -> None:
    d, t, _st, r, first = delivery
    await d.deliver(first, [r])
    t.error = DeliveryError("bad_request:message to edit not found", permanent=True)
    await d.deliver(replace(first, message_ids=[11], translation_html="Second", append_after=10), [r])
    assert t.sends == ["First", "Second"]


async def test_edit_invalidates_cached_text(delivery: Any) -> None:
    d, t, _st, r, first = delivery
    await d.deliver(first, [r])
    await d.update(replace(first, translation_html="Corrected", edited=True), [r])
    await d.deliver(replace(first, message_ids=[11], translation_html="Second", append_after=10), [r])
    assert len(t.sends) == 2
    assert t.edits == [(1, "✏️ Corrected")]


async def test_bubble_window_expires(delivery: Any) -> None:
    d, t, _st, r, first = delivery
    await d.deliver(first, [r])
    d._bubbles[(-1001, None, 1)].ts -= 21
    await d.deliver(replace(first, message_ids=[11], translation_html="Second", append_after=10), [r])
    assert t.sends == ["First", "Second"] and not t.edits


async def test_eviction_during_append_still_records_delivery(delivery: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    d, t, st, r, first = delivery
    await d.deliver(first, [r])
    edit = t.edit_ephemeral

    async def evict(*args: Any) -> None:
        d._bubbles.clear()
        await edit(*args)

    monkeypatch.setattr(t, "edit_ephemeral", evict)
    await d.deliver(replace(first, message_ids=[11], translation_html="Second", append_after=10), [r])
    assert t.edits == [(1, "First\nSecond")]
    assert (await st.deliveries_for(-1001, 11))[0]["status"] == "sent"


def test_long_emoji_reply_preserves_quote_and_notice() -> None:
    from translator_bot.delivery import format_delivery
    from translator_bot.textutil import html_to_plain

    d = Delivery(-1, "Group", None, [11], "A", "my", "en", "\U0001f600" * 3000,
                 "text", parent_excerpt="Question & answer")
    html = format_delivery(d, notice="Private")
    assert "<blockquote>Question &amp; answer</blockquote>" in html
    assert html.endswith("<i>Private</i>")
    assert len(html_to_plain(html).encode("utf-16-le")) // 2 <= 4096
