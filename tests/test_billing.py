"""Private-chat plan with Telegram Stars, end to end (mocked Telegram + providers):
free allowance -> paywall -> invoice -> pre-checkout validation -> payment credited exactly once -> refund."""

from __future__ import annotations

import itertools
import time
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.types import Update

from tests.conftest import BOT_ID, GROUP_ID, OUTSIDER, OWNER, FakeProvider, FakeSession, FakeTelegram
from tests.test_e2e_group import Harness, dm, make_settings
from translator_bot import billing
from translator_bot.app import App
from translator_bot.handlers import build_routers
from translator_bot.storage import User

_uid = itertools.count(50_000)
TEXTS = ["Hola amigos, ¿qué tal?", "¿Dónde estás ahora?", "Nos vemos mañana temprano", "¿Vienes a la fiesta?"]


@pytest.fixture
async def bh(tmp_path: Path, detector: Any) -> Any:
    tg = FakeTelegram(members={GROUP_ID: {OWNER, OUTSIDER, BOT_ID}}, admins={GROUP_ID})
    gemini = FakeProvider("gemini")
    app = App(make_settings(tmp_path, FREE_PRIVATE_MESSAGES=2, PRIVATE_PER_MIN=100), session=FakeSession(tg),
              providers={"gemini": gemini}, detector=detector)
    await app.start()
    dp = Dispatcher()
    for r in build_routers(app):
        dp.include_router(r)
    yield Harness(app, dp, tg, gemini, gemini)
    await app.stop()


async def feed(h: Harness, **kind: Any) -> None:
    await h.dp.feed_update(h.app.bot, Update.model_validate({"update_id": next(_uid), **kind}))


def press(uid: int, data: str) -> dict[str, Any]:
    return {"callback_query": {"id": str(next(_uid)), "from": {"id": uid, "is_bot": False, "first_name": "Pat"},
                               "chat_instance": "ci", "data": data,
                               "message": {"message_id": 1, "date": int(time.time()),
                                           "chat": {"id": uid, "type": "private"}}}}


def checkout(uid: int, payload: str, amount: int, currency: str = "XTR") -> dict[str, Any]:
    return {"pre_checkout_query": {"id": str(next(_uid)), "from": {"id": uid, "is_bot": False, "first_name": "Pat"},
                                   "currency": currency, "total_amount": amount, "invoice_payload": payload}}


def paid_msg(uid: int, payload: str, amount: int, charge: str) -> dict[str, Any]:
    return {"message": dm(uid, mid=next(_uid), successful_payment={
        "currency": "XTR", "total_amount": amount, "invoice_payload": payload,
        "telegram_payment_charge_id": charge, "provider_payment_charge_id": ""})}


def texts_to(h: Harness, uid: int) -> list[str]:
    return [c.params["text"] for c in h.tg.dms_to(uid)]


async def test_free_allowance_then_paywall_then_pack_purchase(bh: Harness) -> None:
    for i, t in enumerate(TEXTS[:2]):
        await bh.send(dm(OUTSIDER, t, mid=10 + i))
    await bh.idle()
    assert sum(t.startswith("[en]") for t in texts_to(bh, OUTSIDER)) == 2
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and u.private_used == 2

    jobs = len(bh.gemini.jobs)
    await bh.send(dm(OUTSIDER, TEXTS[2], mid=12))  # allowance used up -> paywall, no provider call
    await bh.idle()
    last = bh.tg.dms_to(OUTSIDER)[-1]
    assert "used your 2 free" in last.params["text"] and len(bh.gemini.jobs) == jobs
    assert {b["callback_data"] for row in last.params["reply_markup"]["inline_keyboard"] for b in row} == \
        {"pay:pack", "pay:life"}

    await feed(bh, **press(OUTSIDER, "pay:pack"))
    [inv] = [c for c in bh.tg.calls if c.method == "SendInvoice"]
    assert inv.params["currency"] == "XTR" and inv.params["prices"] == [{"label": "1,000 translations",
                                                                        "amount": 100}]
    assert inv.params["payload"] == f"pack:{OUTSIDER}:100" and "provider_token" not in inv.params

    await feed(bh, **checkout(OUTSIDER, inv.params["payload"], 100))
    [ans] = [c for c in bh.tg.calls if c.method == "AnswerPreCheckoutQuery"]
    assert ans.params["ok"] is True

    await feed(bh, **paid_msg(OUTSIDER, inv.params["payload"], 100, "ch-1"))
    await feed(bh, **paid_msg(OUTSIDER, inv.params["payload"], 100, "ch-1"))  # Telegram retry: credited once
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and u.private_credit == 1000
    assert any("+1,000 translations" in t for t in texts_to(bh, OUTSIDER))
    assert any("already on your account" in t for t in texts_to(bh, OUTSIDER))
    assert any("paid 100" in c.params["text"] and "/refund ch-1" in c.params["text"] for c in bh.tg.dms_to(OWNER))

    await bh.send(dm(OUTSIDER, TEXTS[3], mid=13))  # unlocked again
    await bh.idle()
    assert texts_to(bh, OUTSIDER)[-1].startswith("[en]")


@pytest.mark.parametrize(("payload", "amount", "currency"), [
    (f"pack:{OUTSIDER}:50", 50, "XTR"),  # price no longer matches the plan
    (f"pack:{OWNER}:100", 100, "XTR"),  # someone else's invoice
    (f"pack:{OUTSIDER}:100", 100, "USD"),  # not Stars
    ("garbage", 100, "XTR"),
])
async def test_pre_checkout_rejects_invalid_invoices(bh: Harness, payload: str, amount: int, currency: str) -> None:
    await feed(bh, **checkout(OUTSIDER, payload, amount, currency))
    [ans] = [c for c in bh.tg.calls if c.method == "AnswerPreCheckoutQuery"]
    assert ans.params["ok"] is False and ans.params["error_message"]


async def test_lifetime_purchase_unlimited_and_refund(bh: Harness) -> None:
    payload = billing.payload(billing.LIFETIME, OUTSIDER, 10000)
    await feed(bh, **paid_msg(OUTSIDER, payload, 10000, "ch-life"))
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and u.lifetime
    await bh.app.storage.upsert_user(OUTSIDER)
    for i, t in enumerate(TEXTS):  # far beyond the free 2
        await bh.send(dm(OUTSIDER, t, mid=30 + i))
    await bh.idle()
    assert sum(t.startswith("[en]") for t in texts_to(bh, OUTSIDER)) == 4
    # a second lifetime purchase is refused before payment
    await feed(bh, **checkout(OUTSIDER, payload, 10000))
    assert [c for c in bh.tg.calls if c.method == "AnswerPreCheckoutQuery"][-1].params["ok"] is False

    await bh.send(dm(OWNER, "/refund ch-life", mid=40))
    assert any(c.method == "RefundStarPayment" and c.params["telegram_payment_charge_id"] == "ch-life"
               for c in bh.tg.calls)
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and not u.lifetime
    await bh.send(dm(OWNER, "/refund ch-life", mid=41))
    assert "Already refunded" in texts_to(bh, OWNER)[-1]


async def test_mismatched_payment_is_not_credited_and_owner_is_told(bh: Harness) -> None:
    await feed(bh, **paid_msg(OUTSIDER, f"life:{OUTSIDER}:10000", 100, "ch-bad"))  # amount doesn't match payload
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is None or not u.lifetime
    assert any("Unexpected Stars payment" in c.params["text"] for c in bh.tg.dms_to(OWNER))


async def test_group_approval_does_not_make_private_chat_free(bh: Harness) -> None:
    await bh.app.storage.set_approved(OUTSIDER, True, by=OWNER)
    for i, t in enumerate(TEXTS):
        await bh.send(dm(OUTSIDER, t, mid=60 + i))
    await bh.idle()
    out = texts_to(bh, OUTSIDER)
    assert sum(t.startswith("[en]") for t in out) == 2 and any("used your 2 free" in t for t in out)


async def test_free_pass_is_unlimited_and_not_counted(bh: Harness) -> None:
    await bh.send(dm(OWNER, f"/approvefree {OUTSIDER}", mid=59))
    assert "Free for life" in texts_to(bh, OWNER)[-1]
    for i, t in enumerate(TEXTS):
        await bh.send(dm(OUTSIDER, t, mid=60 + i))
    await bh.idle()
    assert sum(t.startswith("[en]") for t in texts_to(bh, OUTSIDER)) == 4
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and u.free_pass and u.private_used == 0  # never counted
    await bh.send(dm(OUTSIDER, "/buy", mid=70))
    assert "free for you" in texts_to(bh, OUTSIDER)[-1]
    await bh.send(dm(OWNER, f"/revokefree {OUTSIDER}", mid=71))
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and not u.free_pass


async def test_owner_grants_free_pass_by_replying_in_group(bh: Harness) -> None:
    from tests.conftest import group_message

    await bh.app.storage.upsert_chat(GROUP_ID, title="G", type="supergroup", enabled=True, bot_is_admin=True)
    target = group_message("hola", sender=OUTSIDER)
    cmd = group_message("/approvefree", sender=OWNER, reply_to=target)
    cmd["entities"] = [{"type": "bot_command", "offset": 0, "length": 12}]
    await bh.send(cmd)
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and u.free_pass
    [note] = bh.tg.ephemeral_to(OWNER)  # only the owner sees the confirmation
    assert "free for life" in note.params["text"] and bh.tg.public_group_output() == []
    # a non-owner can't grant it
    cmd2 = group_message("/approvefree", sender=OUTSIDER, reply_to=group_message("x", sender=OWNER))
    cmd2["entities"] = [{"type": "bot_command", "offset": 0, "length": 12}]
    await bh.send(cmd2)
    owner = await bh.app.storage.get_user(OWNER)
    assert owner is None or not owner.free_pass


async def test_burst_cannot_overshoot_the_allowance(bh: Harness) -> None:
    import asyncio

    await asyncio.gather(*(bh.send(dm(OUTSIDER, t, mid=80 + i)) for i, t in enumerate(TEXTS)))
    await bh.idle()
    out = texts_to(bh, OUTSIDER)
    assert sum(t.startswith("[en]") for t in out) == 2 and sum("free private translations" in t for t in out) == 2


async def test_forgetme_keeps_purchases_and_usage(bh: Harness) -> None:
    await bh.app.storage.record_payment("ch-k", OUTSIDER, billing.PACK, 100, 1000)
    await bh.app.storage.add_private_use(OUTSIDER)
    await bh.app.storage.upsert_user(OUTSIDER, free_pass=True)
    await bh.app.storage.delete_user_data(OUTSIDER)
    u = await bh.app.storage.get_user(OUTSIDER)
    assert u is not None and u.private_credit == 1000 and u.private_used == 1 and u.free_pass


def test_plan_math() -> None:
    plan = billing.Plan(free=1000)
    assert billing.remaining(plan, None) == 1000
    assert billing.remaining(plan, User(user_id=1, private_used=1200, private_credit=1000)) == 800
    assert billing.remaining(plan, User(user_id=1, private_used=99999, lifetime=True)) is None
    assert billing.parse_payload("pack:5:100") == ("pack", 5, 100)
    for bad in ("pack:x:100", "gift:5:100", "pack:5", "pack:-5:100", "pack:5:0"):
        assert billing.parse_payload(bad) is None
