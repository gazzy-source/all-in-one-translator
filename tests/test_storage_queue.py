from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from translator_bot.metrics import Metrics
from translator_bot.recipients import MembershipCache, RecipientResolver
from translator_bot.storage import Storage
from translator_bot.workqueue import BurstCoalescer, BurstItem, Lane


@pytest.fixture
async def st(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "db.sqlite3")
    await s.open()
    yield s
    await s.close()


async def test_migrations_idempotent(tmp_path: Path) -> None:
    s = Storage(tmp_path / "x.sqlite3")
    await s.open()
    assert await s.migrate() == 5
    await s.close()
    s2 = Storage(tmp_path / "x.sqlite3")
    await s2.open()
    assert await s2.migrate() == 5
    await s2.close()


async def test_user_prefs_persist_and_isolated(tmp_path: Path) -> None:
    s = Storage(tmp_path / "p.sqlite3")
    await s.open()
    await s.upsert_user(1, lang="hi")
    await s.upsert_user(2, lang="es")
    await s.upsert_user(1, lang="ja")
    await s.close()
    s = Storage(tmp_path / "p.sqlite3")  # survives restart
    await s.open()
    u1, u2 = await s.get_user(1), await s.get_user(2)
    assert u1 and u1.lang == "ja" and u2 and u2.lang == "es"
    await s.close()


async def test_upsert_rejects_unknown_fields(st: Storage) -> None:
    with pytest.raises(ValueError):
        await st.upsert_user(1, **{"lang=1; DROP TABLE users; --": "x"})


async def test_claim_is_idempotent_under_concurrency(st: Storage) -> None:
    results = await asyncio.gather(*(st.claim_message(-1, 5, None, 9, "h") for _ in range(10)))
    assert results.count(True) == 1


async def test_approval_and_forgetme(st: Storage) -> None:
    await st.set_approved(5, True, by=1)
    await st.upsert_user(5, lang="hi", dm_fallback=True)
    await st.record_delivery(-1, 10, 5, channel="ephemeral", status="sent", lang="hi", ephemeral_id=3)
    await st.delete_user_data(5)
    u = await st.get_user(5)
    assert u is not None and u.approved and u.lang is None and not u.dm_fallback  # owner decision kept
    assert await st.deliveries_for(-1, 10) == []


async def test_retention_cleanup(st: Storage) -> None:
    await st.claim_message(-1, 1, None, 2, "h")
    await st.put_media("u1", has_text=False, text=None, source_lang=None, confidence=0, translations={})
    await st.db.execute("UPDATE messages SET created_at=?", (int(time.time()) - 10_000,))
    await st.db.commit()
    n = await st.cleanup(message_retention_s=3600, media_ttl_s=86400)
    assert n["messages"] == 1 and n["media"] == 0


async def test_chat_migration(st: Storage) -> None:
    await st.upsert_chat(-5, title="old")
    await st.set_muted(1, -5, True)
    await st.migrate_chat_id(-5, -1005)
    assert (await st.get_chat(-1005)) is not None and 1 in await st.muted_users(-1005)


async def test_recipient_resolution(st: Storage) -> None:
    members = {(-1, 1), (-1, 2), (-1, 3)}

    async def check(chat: int, uid: int) -> bool:
        return (chat, uid) in members

    r = RecipientResolver(st, MembershipCache(check), owner_ids=[1], default_lang="en")
    assert [x.user_id for x in await r.resolve(-1, sender_id=9)] == [1]  # OWNER_ONLY by default
    await st.set_approved(2, True, by=1)
    await st.set_approved(4, True, by=1)  # approved but not a member of this group
    await st.upsert_user(2, lang="hi")
    got = {x.user_id: x for x in await r.resolve(-1, sender_id=9)}
    assert set(got) == {1, 2} and got[2].lang == "hi" and got[1].using_default and got[1].notice_pending
    assert {x.user_id for x in await r.resolve(-1, sender_id=2)} == {1}  # sender excluded
    r.onboarding_required = True
    await st.upsert_user(2, onboarded=True)
    assert {x.user_id for x in await r.resolve(-1, sender_id=9)} == {2}


async def test_membership_check_fails_closed(st: Storage) -> None:
    async def broken(chat: int, uid: int) -> bool:
        raise RuntimeError("telegram down")

    m = MembershipCache(broken)
    assert await m.is_member(-1, 1) is False


# --- queue ---------------------------------------------------------------------------------------


async def test_lane_round_robin_fairness() -> None:
    order: list[int] = []
    lane = Lane("t", workers=1, max_total=100, per_chat_max=50, job_timeout=5, metrics=Metrics())
    for i in range(5):
        await lane.submit(1, lambda i=i: _rec(order, 1))
    await lane.submit(2, lambda: _rec(order, 2))
    lane.start()
    await asyncio.sleep(0.1)
    await lane.stop()
    assert order.index(2) <= 1  # busy chat 1 can't starve chat 2


async def _rec(order: list[int], v: int) -> None:
    order.append(v)


async def test_lane_shedding_dedupe_and_timeout() -> None:
    m = Metrics()
    lane = Lane("t", workers=1, max_total=3, per_chat_max=2, job_timeout=0.05, metrics=m)

    async def slow() -> None:
        await asyncio.sleep(1)

    assert await lane.submit(1, slow, key="a")
    assert not await lane.submit(1, slow, key="a")  # duplicate
    assert await lane.submit(1, slow)
    assert not await lane.submit(1, slow)  # per-chat cap
    assert await lane.submit(2, slow)
    assert not await lane.submit(3, slow)  # global cap
    lane.start()
    await asyncio.sleep(0.4)
    await lane.stop(drain_timeout=0.5)
    assert m.counters["queue.t.timeout"] >= 1 and m.counters["queue.t.shed"] == 2


async def test_burst_coalescer_window_and_limits() -> None:
    flushed: list[list[int]] = []

    async def cb(key: object, items: list[BurstItem], meta: object) -> None:
        flushed.append([i.message_id for i in items])

    b = BurstCoalescer(cb, window=0.05, max_items=3)
    k = (1, None, 7)
    await b.add(k, BurstItem(1, "a", "a"), None)
    await b.add(k, BurstItem(2, "b", "b"), None)
    assert b.update_pending(k, 2, "B", "B")
    await asyncio.sleep(0.1)
    assert flushed == [[1, 2]]
    for i in range(3, 6):
        await b.add(k, BurstItem(i, "x", "x"), None)
    assert flushed[-1] == [3, 4, 5]  # flushed immediately at max_items


async def test_burst_disabled() -> None:
    flushed: list[int] = []

    async def cb(key: object, items: list[BurstItem], meta: object) -> None:
        flushed.extend(i.message_id for i in items)

    b = BurstCoalescer(cb, window=0)
    await b.add((1, None, 1), BurstItem(9, "x", "x"), None)
    assert flushed == [9]


async def test_online_backup_while_open(tmp_path: Path) -> None:
    from translator_bot.backup import backup

    st = Storage(tmp_path / "live.sqlite3")
    await st.open()
    try:
        await st.upsert_chat(-100, title="Group", enabled=True)  # bot running: WAL has uncheckpointed data
        out = backup(tmp_path / "live.sqlite3", tmp_path / "backups", keep=2)
        import sqlite3

        con = sqlite3.connect(out)
        assert con.execute("SELECT title FROM chats WHERE chat_id=-100").fetchone()[0] == "Group"
        con.close()
        for _ in range(3):
            backup(tmp_path / "live.sqlite3", tmp_path / "backups", keep=2)
        assert len(list((tmp_path / "backups").glob("translator-*.sqlite3"))) <= 2
    finally:
        await st.close()


async def test_expired_timer_cannot_flush_next_burst() -> None:
    batches = []

    async def record(key, items, meta):
        batches.append([item.message_id for item in items])

    b = BurstCoalescer(record, window=60)
    key = (1, None, 1)
    await b.add(key, BurstItem(1, "a", "a"), None)
    b._pending[key][2].cancel()
    b._spawn_flush(key)
    await b.add(key, BurstItem(2, "b", "b"), None)
    await asyncio.sleep(0)
    assert batches == [[1]]
    await b.flush_all()
    assert batches == [[1], [2]]
