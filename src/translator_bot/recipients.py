"""Who receives translations of a given group message, and in which language.

Recipients = (owners ∪ users approved by a bot owner ∪ users approved by this group's owner/admins) who are
  * current members of the group (verified with getChatMember, short-lived cache),
  * not paused globally and not muted for this group,
  * not the author of the message (you never get your own message translated back to you),
  * onboarded, unless onboarding is optional.
Language: their choice, else the language they write in (`lang_guess`), else their Telegram app language,
else the default; when not chosen explicitly they are told once how to change it.
A failure or setting for one recipient never affects another: each recipient is resolved independently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from translator_bot.languages import same_language
from translator_bot.storage import Storage, User

log = logging.getLogger(__name__)

MemberCheck = Callable[[int, int], Awaitable[bool]]  # (chat_id, user_id) -> is current member


@dataclass(frozen=True, slots=True)
class Recipient:
    user_id: int
    lang: str
    using_default: bool  # never chose a language -> English default (user informed once)
    dm_fallback: bool  # consented to private-chat fallback if ephemeral delivery fails
    notice_pending: bool
    also_reads: frozenset[str] = frozenset()  # languages they read anyway: never translated for them
    show_original: bool = False  # personal preference: quote the original above the translation

    def reads(self, lang: str | None) -> bool:
        """Explicit additional reading languages; Chinese script variants stay distinct."""
        return any(same_language(lang, c) for c in self.also_reads)


class MembershipCache:
    def __init__(self, check: MemberCheck, ttl_s: float = 600, negative_ttl_s: float = 120,
                 max_items: int = 100_000) -> None:
        self._check = check
        self.ttl_s, self.negative_ttl_s, self.max_items = ttl_s, negative_ttl_s, max_items
        self._cache: dict[tuple[int, int], tuple[float, bool]] = {}

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None:
            ts, val = hit
            if now - ts < (self.ttl_s if val else self.negative_ttl_s):
                return val
        try:
            val = await self._check(chat_id, user_id)
        except Exception:  # API hiccup: fail closed (no delivery) but don't cache long
            log.warning("membership check failed", extra={"chat_id": chat_id})
            self._cache[key] = (now - self.negative_ttl_s + 15, False)
            return False
        if len(self._cache) >= self.max_items:
            self._cache.clear()
        self._cache[key] = (now, val)
        return val

    def invalidate(self, chat_id: int, user_id: int | None = None) -> None:
        if user_id is None:
            for k in [k for k in self._cache if k[0] == chat_id]:
                del self._cache[k]
        else:
            self._cache.pop((chat_id, user_id), None)

    def set(self, chat_id: int, user_id: int, is_member: bool) -> None:
        self._cache[(chat_id, user_id)] = (time.monotonic(), is_member)


class RecipientResolver:
    def __init__(self, storage: Storage, membership: MembershipCache, owner_ids: list[int], *,
                 default_lang: str = "en", onboarding_required: bool = False) -> None:
        self.storage = storage
        self.membership = membership
        self.owner_ids = list(owner_ids)
        self.default_lang = default_lang
        self.onboarding_required = onboarding_required

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    async def is_approved(self, user_id: int) -> bool:
        """Globally approved: a bot owner, or approved by a bot owner. Gates private-chat caps, inline mode and
        everything outside groups. Group-admin approval is NOT counted (anyone can admin their own group)."""
        if self.is_owner(user_id):
            return True
        u = await self.storage.get_user(user_id)
        return bool(u and u.approved)

    async def has_group_access(self, user_id: int) -> bool:
        """Reader somewhere: globally approved, or approved by the admins of at least one group (for the
        group-related private commands only: /groups, /pause, help display)."""
        if await self.is_approved(user_id) or await self.storage.is_chat_approved(user_id):
            return True
        for chat in await self.storage.active_chats():
            if chat.enabled and chat.settings.get("everyone"):
                seen = any(u.user_id == user_id for u in await self.storage.participants(chat.chat_id))
                if seen and await self.membership.is_member(chat.chat_id, user_id):
                    return True
        return False

    async def everyone_mode(self, chat_id: int) -> bool:
        """The group's owner approved everyone here (/approveall)."""
        chat = await self.storage.get_chat(chat_id)
        return bool(chat and chat.settings.get("everyone"))

    async def is_approved_in(self, chat_id: int, user_id: int) -> bool:
        """Approved for this group: owner, approved by a bot owner, approved by this group's owner/admins, or the
        group's admins approved everyone."""
        if self.is_owner(user_id) or await self.everyone_mode(chat_id):
            return True
        u = await self.storage.get_user(user_id)
        return bool(u and u.approved) or await self.storage.is_chat_approved(user_id, chat_id)

    async def candidates(self, chat_id: int | None = None) -> list[User]:
        users = {u.user_id: u for u in await self.storage.approved_users()}
        if chat_id is not None:
            for u in await self.storage.chat_approved_users(chat_id):
                users.setdefault(u.user_id, u)
            if await self.everyone_mode(chat_id):
                for u in await self.storage.participants(chat_id):
                    users.setdefault(u.user_id, u)
        for oid in self.owner_ids:
            if oid not in users:
                users[oid] = await self.storage.get_user(oid) or User(user_id=oid, approved=True)
        return list(users.values())

    async def resolve(self, chat_id: int, sender_id: int | None) -> list[Recipient]:
        muted = await self.storage.muted_users(chat_id)
        users = [u for u in await self.candidates(chat_id)
                 if u.user_id != sender_id and not u.paused and u.user_id not in muted
                 and not (self.onboarding_required and not u.onboarded)]
        # membership checks in parallel (each uncached one is a getChatMember round trip, ~0.1-0.3 s), bounded
        sem = asyncio.Semaphore(8)

        async def member(uid: int) -> bool:
            async with sem:
                return await self.membership.is_member(chat_id, uid)

        flags = await asyncio.gather(*(member(u.user_id) for u in users))
        out: list[Recipient] = []
        for u, is_member in zip(users, flags, strict=True):
            if not is_member:
                continue
            using_default = not u.lang  # automatic language (or the default): told once how to change it
            out.append(Recipient(
                user_id=u.user_id, lang=u.effective_lang or self.default_lang, using_default=using_default,
                dm_fallback=u.dm_fallback and u.started, notice_pending=using_default and not u.default_notice_sent,
                also_reads=u.also_reads_set, show_original=u.show_original,
            ))
        return out
