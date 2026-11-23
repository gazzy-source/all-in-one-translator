"""Private-chat plan: a free allowance of translations, then Telegram Stars (XTR) purchases.

* Every user gets `FREE_PRIVATE_MESSAGES` (1000) private-chat translations; only delivered translations count.
* Then: a pack of `STARS_PACK_SIZE` (1000) more for `STARS_PACK_PRICE` (100 ⭐), or unlimited for life for
  `STARS_LIFETIME_PRICE` (10000 ⭐).
* Owners, and users the owner granted a free pass (`/approvefree`), are never charged and never counted.
  Group approval (`/approve`) is separate: it doesn't make private-chat use free.
* Group translations are not metered (they're for approved members only).

Payments follow Telegram's digital-goods rules: Stars only (no provider token), every invoice is re-validated
at pre-checkout, a payment is credited exactly once (by its telegram_payment_charge_id), refunds revert the grant,
and /paysupport reaches the owner.
"""

from __future__ import annotations

from dataclasses import dataclass

from translator_bot.storage import User

PACK = "pack"
LIFETIME = "life"


@dataclass(frozen=True, slots=True)
class Plan:
    enabled: bool = True
    free: int = 1000
    pack_size: int = 1000
    pack_price: int = 100
    lifetime_price: int = 10000

    def price(self, product: str) -> int | None:
        return {PACK: self.pack_price, LIFETIME: self.lifetime_price}.get(product)

    def grant(self, product: str) -> int:
        """Translations added by a product (0 = lifetime, unlimited)."""
        return self.pack_size if product == PACK else 0


def is_exempt(plan: Plan, *, is_owner: bool, user: User | None) -> bool:
    """Never charged or counted: paywall off, owners, and owner-granted free passes (/approvefree)."""
    return not plan.enabled or is_owner or bool(user and user.free_pass)


def remaining(plan: Plan, user: User | None) -> int | None:
    """Private translations left, or None for unlimited (lifetime)."""
    if user is not None and user.lifetime:
        return None
    used = user.private_used if user else 0
    credit = user.private_credit if user else 0
    return max(0, plan.free + credit - used)


def has_paid(user: User | None) -> bool:
    return bool(user and (user.lifetime or user.free_pass or user.private_credit > 0))


def payload(product: str, user_id: int, price: int) -> str:
    """Invoice payload: binds the purchase to the product, the buyer and the price shown on the invoice."""
    return f"{product}:{user_id}:{price}"


def parse_payload(value: str) -> tuple[str, int, int] | None:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] not in (PACK, LIFETIME):
        return None
    try:
        uid, price = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if uid <= 0 or price <= 0:
        return None
    return parts[0], uid, price


def fmt(n: int) -> str:
    return f"{n:,}"


def status_line(plan: Plan, user: User | None) -> str:
    """One line for /help and /buy."""
    if user is not None and user.free_pass:
        return "🎁 <b>Free for life</b> — unlimited private translations"
    left = remaining(plan, user)
    if left is None:
        return "♾ <b>Lifetime</b> — unlimited private translations"
    if has_paid(user):
        return f"🎟 <b>{fmt(left)}</b> private translations left"
    return f"🎟 <b>{fmt(left)}</b> of {fmt(plan.free)} free private translations left"


def offer_text(plan: Plan) -> str:
    return (f"• <b>{fmt(plan.pack_size)} more</b> — {fmt(plan.pack_price)} ⭐\n"
            f"• <b>Unlimited, forever</b> — {fmt(plan.lifetime_price)} ⭐\n\n"
            "<i>Paid with Telegram Stars. Group translations stay free for approved members.</i>")


def paywall_text(plan: Plan) -> str:
    return f"🎟 <b>You've used your {fmt(plan.free)} free private translations.</b>\n\n" + offer_text(plan)


def product_title(plan: Plan, product: str) -> tuple[str, str]:
    if product == LIFETIME:
        return "Lifetime unlimited", "Unlimited private-chat translations, forever."
    return (f"{fmt(plan.pack_size)} translations",
            f"{fmt(plan.pack_size)} more private-chat translations (text, stickers, GIFs, photos, voice).")
