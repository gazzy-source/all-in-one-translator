"""aiogram routers: group (silent), private (1:1 translator, onboarding & settings), callbacks, inline mode."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import re
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChatMember,
    CallbackQuery,
    ChatMemberUpdated,
    EphemeralMessageParameters,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    LabeledPrice,
    LinkPreviewOptions,
    Message,
    PreCheckoutQuery,
    ReplyParameters,
)

from translator_bot import billing, ui
from translator_bot.app import App
from translator_bot.detect import Verdict
from translator_bot.eligibility import Eligible, Skip, classify
from translator_bot.languages import lang_name, resolve_language, suggest_languages
from translator_bot.pipeline import PrivateResult, html_escape_truncate
from translator_bot.providers.base import TranslationJob
from translator_bot.textutil import ContentKind, html_to_plain, normalize_for_key, profile_text, truncate

log = logging.getLogger(__name__)
_INT_RE = re.compile(r"-?\d{1,20}")  # callback/command ids: "--5" or "x" must not reach int()

GROUP = F.chat.type.in_({"group", "supergroup"})
PRIVATE = F.chat.type == "private"

PRIVATE_COMMANDS = [
    BotCommand(command="start", description="Onboarding and current status"),
    BotCommand(command="language", description="Change your translation language"),
    BotCommand(command="settings", description="Personal preferences"),
    BotCommand(command="groups", description="Groups where you receive translations"),
    BotCommand(command="pause", description="Pause your translations"),
    BotCommand(command="resume", description="Resume your translations"),
    BotCommand(command="help", description="How it works"),
    BotCommand(command="privacy", description="What is processed and stored"),
    BotCommand(command="buy", description="More private translations (Telegram Stars)"),
    BotCommand(command="paysupport", description="Help with a payment"),
]
OWNER_GROUP_COMMANDS = [  # registered per owner per group; hidden from everyone else
    BotCommand(command="approve", description="Reply to a member: approve as recipient", is_ephemeral=True),
    BotCommand(command="revoke", description="Reply to a member: revoke recipient", is_ephemeral=True),
    BotCommand(command="approvefree", description="Reply to a member: free private chat for life", is_ephemeral=True),
    BotCommand(command="revokefree", description="Reply to a member: remove free pass", is_ephemeral=True),
    BotCommand(command="check", description="Verify translator setup", is_ephemeral=True),
    BotCommand(command="glossary", description="term = rendering  |  -term", is_ephemeral=True),
    BotCommand(command="mute", description="Stop my translations here", is_ephemeral=True),
    BotCommand(command="unmute", description="Resume my translations here", is_ephemeral=True),
]


def build_routers(app: App) -> list[Router]:
    return [_group_router(app), _private_router(app), _callback_router(app), _inline_router(app)]


async def register_commands(app: App) -> None:
    await app.bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())


async def register_owner_group_commands(app: App, chat_id: int) -> None:
    for oid in app.s.owner_ids:
        with contextlib.suppress(TelegramAPIError):
            await app.bot.set_my_commands(OWNER_GROUP_COMMANDS,
                                          scope=BotCommandScopeChatMember(chat_id=chat_id, user_id=oid))


async def ephemeral_reply(bot: Bot, msg: Message, text: str) -> None:
    """Answer a command in a group so that ONLY its sender sees the answer."""
    if msg.from_user is None:
        return
    reply = ReplyParameters(ephemeral_message_id=msg.ephemeral_message_id) if msg.ephemeral_message_id else None
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(chat_id=msg.chat.id, text=text, reply_parameters=reply,
                               message_thread_id=msg.message_thread_id if msg.is_topic_message else None,
                               ephemeral_message_parameters=EphemeralMessageParameters(
                                   receiver_user_id=msg.from_user.id))


# --- group -----------------------------------------------------------------------------------------


def _group_router(app: App) -> Router:
    r = Router(name="group")

    @r.my_chat_member(GROUP)
    async def bot_membership(ev: ChatMemberUpdated) -> None:
        status = ev.new_chat_member.status
        chat_id = ev.chat.id
        if status in ("left", "kicked"):
            await app.storage.upsert_chat(chat_id, active=False, bot_is_admin=False)
            app.membership.invalidate(chat_id)
            app.context.forget_chat(chat_id)
            if app.pipeline:
                app.pipeline.invalidate_chat(chat_id)
            await app.notify_owners(f"ℹ️ Removed from <b>{html.escape(ev.chat.title or str(chat_id))}</b>.")
            return
        existing = await app.storage.get_chat(chat_id)
        actor = ev.from_user.id if ev.from_user else 0
        if existing is None:
            policy = app.s.enable_new_groups
            enabled = policy == "all" or (policy == "owner_added" and actor in app.s.owner_ids)
            await app.storage.upsert_chat(chat_id, title=ev.chat.title, type=ev.chat.type, enabled=enabled,
                                          active=True)
        await register_owner_group_commands(app, chat_id)
        try:
            report = await app.verify_setup(chat_id)
        except TelegramAPIError:
            log.warning("setup check failed", extra={"chat_id": chat_id})
            return
        chat = await app.storage.get_chat(chat_id)
        state = "enabled" if chat and chat.enabled else "added by a non-owner — DISABLED until you enable it in /chats"
        who = html.escape(ev.from_user.full_name) if ev.from_user else "someone"
        await app.notify_owners(f"{report.render()}\n\nAdded/updated by {who}. Translation: {state}.")

    @r.chat_member(GROUP)
    async def member_changed(ev: ChatMemberUpdated) -> None:
        st = ev.new_chat_member.status
        is_member = st in ("creator", "administrator", "member") or (
            st == "restricted" and bool(getattr(ev.new_chat_member, "is_member", False)))
        app.membership.set(ev.chat.id, ev.new_chat_member.user.id, is_member)

    @r.message(GROUP, F.migrate_to_chat_id)
    async def migrated(msg: Message) -> None:
        if msg.migrate_to_chat_id:
            await app.storage.migrate_chat_id(msg.chat.id, msg.migrate_to_chat_id)

    @r.message(GROUP, F.text.startswith("/"))
    async def group_command(msg: Message, bot: Bot) -> None:
        """Hidden owner/recipient commands. Never answered publicly; unauthorized users are ignored."""
        if msg.from_user is None or not msg.text:
            return
        head, _, arg = msg.text.partition(" ")
        cmd, _, target = head[1:].partition("@")
        if target and target.lower() != app.bot_username.lower():
            return
        uid = msg.from_user.id
        is_owner = app.resolver.is_owner(uid)
        if cmd in ("approve", "revoke", "approvefree", "revokefree", "check", "glossary") and not is_owner:
            return
        if cmd in ("approvefree", "revokefree"):
            r_msg = msg.reply_to_message
            if (r_msg is None or r_msg.from_user is None or r_msg.from_user.is_bot or r_msg.sender_chat is not None
                    or r_msg.forum_topic_created is not None
                    or (msg.is_topic_message and r_msg.message_id == msg.message_thread_id)):
                await ephemeral_reply(bot, msg, f"Reply to a member's message with /{cmd}.")
                return
            member = r_msg.from_user
            await app.storage.upsert_user(member.id, first_name=member.first_name, free_pass=cmd == "approvefree")
            verb = ("🎁 free for life in private chat (not counted, never charged)" if cmd == "approvefree"
                    else "free pass removed")
            await ephemeral_reply(bot, msg, f"{html.escape(member.full_name)} ({member.id}): {verb}.")
            return
        if cmd in ("approve", "revoke"):
            r_msg = msg.reply_to_message
            # In forum topics Telegram fills reply_to_message with the topic's creation message when the owner
            # didn't reply to anything: never approve/revoke the topic creator by accident.
            if (r_msg is None or r_msg.from_user is None or r_msg.from_user.is_bot or r_msg.sender_chat is not None
                    or r_msg.forum_topic_created is not None
                    or (msg.is_topic_message and r_msg.message_id == msg.message_thread_id)):
                await ephemeral_reply(bot, msg, "Reply to a member's message with /approve or /revoke.")
                return
            member = r_msg.from_user
            await app.storage.upsert_user(member.id, first_name=member.first_name)
            await app.storage.set_approved(member.id, cmd == "approve", by=uid)
            app.membership.set(msg.chat.id, member.id, True)
            verb = "approved ✅ — they now receive private translations" if cmd == "approve" else "revoked ❌"
            await ephemeral_reply(bot, msg, f"{html.escape(member.full_name)} ({member.id}) {verb}.")
        elif cmd == "check":
            report = await app.verify_setup(msg.chat.id)
            await ephemeral_reply(bot, msg, report.render())
        elif cmd == "glossary":
            await ephemeral_reply(bot, msg, await _glossary_cmd(app, msg.chat.id, arg))
        elif cmd in ("mute", "unmute"):
            if not await app.resolver.is_approved(uid):
                return
            await app.storage.set_muted(uid, msg.chat.id, cmd == "mute")
            await ephemeral_reply(bot, msg, "🔕 Translations muted in this group." if cmd == "mute"
                                  else "🔔 Translations resumed in this group.")
        elif cmd in ("start", "language", "settings", "help") and await app.resolver.is_approved(uid):
            await ephemeral_reply(bot, msg, f'Settings live in our private chat: <a href="https://t.me/'
                                            f'{app.bot_username}?start=settings">open</a>.')

    @r.message(GROUP)
    async def group_message(msg: Message) -> None:
        if app.pipeline is not None:
            await app.pipeline.on_message(msg)  # enqueues and returns immediately

    @r.edited_message(GROUP)
    async def group_edit(msg: Message) -> None:
        if app.pipeline is not None:
            await app.pipeline.on_edit(msg)

    return r


async def _glossary_cmd(app: App, chat_id: int, arg: str) -> str:
    arg = arg.strip()
    if not arg:
        items = await app.storage.glossary(chat_id)
        if not items:
            return "Glossary is empty. Add: <code>/glossary term = rendering</code> (use the same word to keep it " \
                   "untranslated). Remove: <code>/glossary -term</code>."
        return "<b>Glossary</b>\n" + "\n".join(f"• {html.escape(t)} → {html.escape(r)}" for t, r in items)
    if arg.startswith("-"):
        await app.storage.set_glossary(chat_id, arg[1:].strip(), None)
        return "Removed."
    term, sep, rendering = arg.partition("=")
    term, rendering = term.strip(), (rendering.strip() if sep else term.strip())
    if not term or len(term) > 64 or len(rendering) > 64:
        return "Usage: <code>/glossary term = rendering</code> (max 64 chars each)."
    await app.storage.set_glossary(chat_id, term, rendering)
    if app.orchestrator:
        app.orchestrator.cache.clear()
    return f"Saved: {html.escape(term)} → {html.escape(rendering)}"


# --- private ---------------------------------------------------------------------------------------


def _private_router(app: App) -> Router:
    r = Router(name="private")
    r.message.filter(PRIVATE)
    awaiting_lang: dict[int, float] = {}  # user id -> when the language prompt was shown (typed names resolve)

    async def access(msg: Message) -> tuple[bool, bool]:
        """(may use the private chat, is approved for group translations). Replies when access is denied."""
        if msg.from_user is None:
            return False, False
        await app.storage.upsert_user(msg.from_user.id, first_name=msg.from_user.first_name, started=True)
        approved = await app.resolver.is_approved(msg.from_user.id)
        if approved or app.s.private_access == "everyone":
            return True, approved
        await msg.answer(ui.ACCESS_DENIED.format(uid=msg.from_user.id))
        return False, False

    async def gate(msg: Message) -> bool:
        """Group-translation features (groups, pause): approved users only."""
        ok, approved = await access(msg)
        if ok and not approved:
            assert msg.from_user is not None
            await msg.answer(ui.ACCESS_DENIED.format(uid=msg.from_user.id))
        return approved

    def prompt_language(uid: int) -> None:
        awaiting_lang[uid] = time.monotonic()
        if len(awaiting_lang) > 10_000:
            awaiting_lang.pop(next(iter(awaiting_lang)))

    # -- Telegram Stars: payment results arrive as service messages in the private chat --------------
    @r.message(F.successful_payment)
    async def paid(msg: Message) -> None:
        sp = msg.successful_payment
        if sp is None or msg.from_user is None:
            return
        uid = msg.from_user.id
        parsed = billing.parse_payload(sp.invoice_payload)
        charge = html.escape(sp.telegram_payment_charge_id)
        if sp.currency != "XTR" or parsed is None or parsed[1] != uid or parsed[2] != sp.total_amount:
            log.error("unexpected payment", extra={"user_id": uid, "amount": sp.total_amount})
            await app.notify_owners(f"⚠️ Unexpected Stars payment from <code>{uid}</code> ({sp.total_amount} ⭐). "
                                    f"Check it, or refund: <code>/refund {charge}</code>")
            await _say(msg, "Payment received — the owner will check it and get back to you.")
            return
        product = parsed[0]
        new = await app.storage.record_payment(sp.telegram_payment_charge_id, uid, product, sp.total_amount,
                                               app.plan.grant(product))
        user = await app.storage.get_user(uid)
        if not new:
            await _say(msg, "✅ This payment is already on your account.\n" + billing.status_line(app.plan, user))
            return
        app.metrics.inc(f"payments.{product}")
        thanks = ("♾ <b>Lifetime unlocked</b> — thank you!" if product == billing.LIFETIME
                  else f"✅ <b>+{billing.fmt(app.plan.grant(product))} translations</b> — thank you!")
        await _say(msg, thanks + "\n" + billing.status_line(app.plan, user))
        who = html.escape(msg.from_user.full_name)
        await app.notify_owners(f"💰 {who} (<code>{uid}</code>) paid {sp.total_amount} ⭐ · {product}\n"
                                f"Refund: <code>/refund {charge}</code>")

    @r.message(F.refunded_payment)
    async def refunded(msg: Message) -> None:
        rp = msg.refunded_payment
        if rp is None:
            return
        await app.storage.mark_refunded(rp.telegram_payment_charge_id)
        user = await app.storage.get_user(msg.from_user.id) if msg.from_user else None
        await _say(msg, "↩️ Your payment was refunded.\n" + billing.status_line(app.plan, user))

    @r.message(Command("buy"))
    async def buy(msg: Message) -> None:
        ok, _approved = await access(msg)
        if not ok:
            return
        assert msg.from_user is not None
        uid = msg.from_user.id
        user = await app.storage.get_user(uid)
        if billing.is_exempt(app.plan, is_owner=app.resolver.is_owner(uid), user=user):
            await msg.answer("🎁 Private translations are free for you — nothing to buy.")
            return
        if user is not None and user.lifetime:
            await msg.answer(billing.status_line(app.plan, user))
            return
        await msg.answer(billing.status_line(app.plan, user) + "\n\n" + billing.offer_text(app.plan),
                         reply_markup=ui.paywall_kb(app.plan))

    @r.message(Command("paysupport"))
    async def paysupport(msg: Message, command: CommandObject) -> None:
        """Required by Telegram for bots selling digital goods."""
        if msg.from_user is None:
            return
        text = (command.args or "").strip()
        if not text:
            await msg.answer("🆘 <b>Payment help</b>\nDescribe the problem after the command, e.g.\n"
                             "<code>/paysupport I paid but didn't get the translations</code>\n"
                             "The owner gets your message and your Telegram ID, and can refund you.")
            return
        u = msg.from_user
        contact = f"@{html.escape(u.username)}" if u.username else "no username"
        await app.notify_owners(f"🆘 Payment support from {html.escape(u.full_name)} ({contact}, "
                                f"<code>{u.id}</code>):\n{html.escape(truncate(text, 1000))}")
        await msg.answer("📨 Sent to the owner — they'll get back to you.")

    @r.message(Command("refund"))
    async def refund(msg: Message, command: CommandObject) -> None:
        """Owner: /refund <telegram_payment_charge_id> - refunds the Stars and removes what was bought."""
        if msg.from_user is None or not app.resolver.is_owner(msg.from_user.id):
            return
        charge = (command.args or "").strip()
        pay = await app.storage.payment(charge) if charge else None
        if pay is None:
            await msg.answer("Usage: <code>/refund &lt;payment id&gt;</code> (the id is in the payment notification).")
            return
        if pay["refunded"]:
            await msg.answer("Already refunded.")
            return
        try:
            await app.bot.refund_star_payment(user_id=int(pay["user_id"]), telegram_payment_charge_id=charge)
        except TelegramAPIError as exc:
            await msg.answer(f"Refund failed: {html.escape(str(exc))[:200]}")
            return
        await app.storage.mark_refunded(charge)
        await msg.answer(f"↩️ Refunded {pay['stars']} ⭐ to <code>{pay['user_id']}</code>; their "
                         f"{html.escape(str(pay['product']))} was removed.")

    @r.message(Command("start"))
    async def start(msg: Message) -> None:
        ok, approved = await access(msg)
        if not ok:
            return
        assert msg.from_user is not None
        user = await app.storage.get_user(msg.from_user.id)
        assert user is not None
        await msg.answer(ui.welcome(user, default_lang=app.s.default_target_lang,
                                    is_owner=app.resolver.is_owner(user.user_id), bot_username=app.bot_username,
                                    approved=approved, inline_ok=approved or app.s.inline_access == "everyone",
                                    max_seconds=int(app.s.max_media_seconds)))
        if not user.onboarded:
            prompt_language(user.user_id)
            await msg.answer(ui.LANGUAGE_PROMPT, reply_markup=ui.language_menu(user.effective_lang or app.s.default_target_lang))

    @r.message(Command("language"))
    async def language(msg: Message) -> None:
        ok, _approved = await access(msg)
        if not ok:
            return
        assert msg.from_user is not None
        user = await app.storage.get_user(msg.from_user.id)
        prompt_language(msg.from_user.id)
        await msg.answer(ui.LANGUAGE_PROMPT, reply_markup=ui.language_menu(
            (user.effective_lang if user else None) or app.s.default_target_lang))

    @r.message(Command("settings"))
    async def settings(msg: Message) -> None:
        ok, _approved = await access(msg)
        if not ok:
            return
        assert msg.from_user is not None
        user = await app.storage.get_user(msg.from_user.id)
        assert user is not None
        await msg.answer(ui.settings_text(user, app.s.default_target_lang),
                         reply_markup=ui.settings_kb(user, app.s.default_target_lang))

    @r.message(Command("groups"))
    async def groups(msg: Message) -> None:
        if not await gate(msg):
            return
        assert msg.from_user is not None
        await _send_groups(app, msg, msg.from_user.id)

    @r.message(Command("pause", "resume"))
    async def pause(msg: Message, command: CommandObject) -> None:
        if not await gate(msg):
            return
        assert msg.from_user is not None
        paused = command.command == "pause"
        await app.storage.upsert_user(msg.from_user.id, paused=paused)
        await msg.answer("⏸ Your translations are paused. /resume to restart." if paused
                         else "▶️ Your translations are active again.")

    @r.message(Command("help"))
    async def help_(msg: Message) -> None:
        ok, approved = await access(msg)
        if not ok:
            return
        assert msg.from_user is not None
        uid = msg.from_user.id
        user = await app.storage.get_user(uid)
        lang = (user.effective_lang if user else None) or app.s.default_target_lang
        groups = await _group_count(app, uid) if approved else None
        await msg.answer(ui.help_text(uid=uid, lang=lang, bot_username=app.bot_username,
                                      approved=approved, private_ok=True,
                                      inline_ok=approved or app.s.inline_access == "everyone",
                                      max_seconds=int(app.s.max_media_seconds),
                                      is_owner=app.resolver.is_owner(uid),
                                      paused=bool(user and user.paused), user=user, groups=groups,
                                      plan_line=None if app.resolver.is_owner(uid) or not app.plan.enabled
                                      else billing.status_line(app.plan, user)),
                         reply_markup=ui.help_kb(lang, approved=approved))

    @r.message(Command("privacy"))
    async def privacy(msg: Message) -> None:
        await msg.answer(ui.PRIVACY)  # available to everyone

    @r.message(Command("forgetme"))
    async def forget(msg: Message) -> None:
        if msg.from_user is None:
            return
        await app.storage.delete_user_data(msg.from_user.id)
        await msg.answer("🗑 Your preferences and delivery records were deleted.")

    # owner commands
    @r.message(Command("approve", "revoke"))
    async def approve(msg: Message, command: CommandObject) -> None:
        if msg.from_user is None or not app.resolver.is_owner(msg.from_user.id):
            await msg.answer(ui.ACCESS_DENIED.format(uid=msg.from_user.id if msg.from_user else 0))
            return
        ids = [int(x) for x in (command.args or "").replace(",", " ").split() if _INT_RE.fullmatch(x)]
        if not ids or any(i <= 0 for i in ids):
            await msg.answer(f"Usage: <code>/{command.command} 123456789 [more ids]</code> — numeric Telegram user "
                             "IDs. Or reply to a member's message in the group with the hidden "
                             f"<code>/{command.command}</code> command.")
            return
        for i in ids:
            await app.storage.set_approved(i, command.command == "approve", by=msg.from_user.id)
        await msg.answer(f"{'Approved' if command.command == 'approve' else 'Revoked'}: "
                         + ", ".join(f"<code>{i}</code>" for i in ids))

    @r.message(Command("approvefree", "revokefree"))
    async def approve_free(msg: Message, command: CommandObject) -> None:
        """Owner: free lifetime private-chat use (never counted, never charged). Separate from /approve."""
        if msg.from_user is None or not app.resolver.is_owner(msg.from_user.id):
            return
        ids = [int(x) for x in (command.args or "").replace(",", " ").split() if _INT_RE.fullmatch(x)]
        if not ids or any(i <= 0 for i in ids):
            users = await app.storage.free_pass_users()
            listing = ", ".join(f"{html.escape(u.first_name or '')} <code>{u.user_id}</code>" for u in users) or "none"
            await msg.answer(f"Usage: <code>/{command.command} 123456789</code> — or reply "
                             f"<code>/{command.command}</code> to someone's message in a group.\n"
                             f"🎁 Free for life: {listing}")
            return
        grant = command.command == "approvefree"
        for i in ids:
            await app.storage.upsert_user(i, free_pass=grant)
        await msg.answer(("🎁 Free for life: " if grant else "Free pass removed: ")
                         + ", ".join(f"<code>{i}</code>" for i in ids))

    @r.message(Command("recipients"))
    async def recipients(msg: Message) -> None:
        if msg.from_user is None or not app.resolver.is_owner(msg.from_user.id):
            return
        users = await app.storage.approved_users()
        await msg.answer(f"<b>Approved recipients</b> ({len(users)}). Owners always receive translations. "
                         "Tap to revoke.", reply_markup=ui.recipients_kb(users))

    @r.message(Command("chats"))
    async def chats(msg: Message) -> None:
        if msg.from_user is None or not app.resolver.is_owner(msg.from_user.id):
            return
        await msg.answer("<b>Groups</b> 🟢 active · 🟡 enabled but bot isn't admin · ⚪ disabled",
                         reply_markup=ui.chats_kb(await app.storage.active_chats()))

    @r.message(Command("status"))
    async def status(msg: Message) -> None:
        if msg.from_user is None or not app.resolver.is_owner(msg.from_user.id):
            return
        await msg.answer(_status_text(app))

    @r.message(F.text)
    async def free_text(msg: Message) -> None:
        ok, approved = await access(msg)
        if not ok or not msg.text:
            return
        assert msg.from_user is not None
        uid = msg.from_user.id
        user = await app.storage.get_user(uid)
        current = (user.effective_lang if user else None) or app.s.default_target_lang
        text = msg.text.strip()
        if text.startswith("/"):
            await msg.answer("Unknown command — see /help.")
            return
        waiting = time.monotonic() - awaiting_lang.pop(uid, -1e9) < 600
        exact = resolve_language(text) if len(text) <= 40 else None
        if exact is not None and not waiting and text.lower() == exact.code.lower():
            exact = None  # bare codes ("hi", "no", "it") are ordinary words unless the language prompt is open
        if exact is not None:
            await msg.answer(f"Translate into <b>{html.escape(exact.label)}</b>?",
                             reply_markup=ui.suggestions_kb([exact], current))
            return
        if waiting and len(text) <= 40:
            hits = suggest_languages(text)
            if hits:
                await msg.answer("Did you mean:", reply_markup=ui.suggestions_kb(hits, current))
                return
        await _private_translate(app, msg, current, approved)

    @r.message()
    async def media(msg: Message) -> None:
        ok, approved = await access(msg)
        if not ok:
            return
        assert msg.from_user is not None
        user = await app.storage.get_user(msg.from_user.id)
        await _private_translate(app, msg, (user.effective_lang if user else None) or app.s.default_target_lang, approved)

    return r


_PRIVATE_REASONS = {
    "same": "✅ Already in <b>{lang}</b>. To translate into another language: /language",
    "no_text": "🤷 I couldn't find any text or speech to translate in that.",
    "too_long": "⏱ That's longer than {secs} s — I translate voice notes and videos up to {secs} s.",
    "failed": "⚠️ Translation is unavailable right now — please try again in a minute.",
    "slow": "⏳ That took too long to read — please send it again in a minute.",
}


_GLOBAL_KEY = 0  # Telegram user ids are positive: key 0 counts all users who aren't approved


def _daily_ok(app: App, uid: int) -> bool:
    """Per-user and total per-day caps on private translations for users who aren't approved (protects the
    groups' free-tier quota). In memory: a restart resets the counters."""
    today = time.strftime("%Y-%m-%d", time.gmtime())

    def count(key: int) -> int:
        day, n = app.private_daily.get(key, (today, 0))
        return n if day == today else 0

    per_user, total = app.s.private_daily_limit, app.s.private_global_daily_limit
    if (per_user and count(uid) >= per_user) or (total and count(_GLOBAL_KEY) >= total):
        return False
    app.private_daily[uid] = (today, count(uid) + 1)
    app.private_daily[_GLOBAL_KEY] = (today, count(_GLOBAL_KEY) + 1)
    if len(app.private_daily) > 50_000:  # bounded: forget other days' counters
        for k in [k for k, (d, _n) in app.private_daily.items() if d != today]:
            del app.private_daily[k]
    return True


async def _say(msg: Message, text: str) -> None:
    with contextlib.suppress(TelegramAPIError):
        await msg.answer(text)


async def _reply_safe(msg: Message, body_html: str) -> None:
    """Reply in the private chat; never silently lose the answer to length or markup problems."""
    if len(body_html) > 4096:
        body_html = html.escape(truncate(html_to_plain(body_html), 4000), quote=False)
    preview = LinkPreviewOptions(is_disabled=True)
    for attempt in range(2):
        try:
            await msg.reply(body_html, link_preview_options=preview)
            return
        except TelegramRetryAfter as exc:
            if attempt or exc.retry_after > 10:
                break
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest as exc:
            if "parse" not in str(exc).lower() and "tag" not in str(exc).lower():
                break
            with contextlib.suppress(TelegramAPIError):  # broken markup: send it as plain text
                await msg.reply(truncate(html_to_plain(body_html), 4096), parse_mode=None,
                                link_preview_options=preview)
                return
            break
        except TelegramAPIError:
            break
    log.warning("private reply failed")


async def _private_translate(app: App, msg: Message, target: str, approved: bool) -> None:
    """1:1 translator: a message sent to the bot privately is translated into the user's language."""
    pipeline = app.pipeline
    if pipeline is None or msg.from_user is None:
        return
    el = classify(msg, bot_id=app.bot_id, settings={}, media_enabled=app.s.media_enabled, private=True)
    if isinstance(el, Skip):
        if el in (Skip.NON_LINGUISTIC, Skip.NO_CONTENT, Skip.UNSUPPORTED, Skip.MEDIA_DISABLED):
            await _say(msg, f"Send me {ui.MEDIA_TYPES} to translate." if app.s.media_enabled
                       else "Send me text or captions to translate.")
        return
    uid = msg.from_user.id
    if not app.private_limiter.try_acquire(uid):
        await _say(msg, "⏳ That's a lot at once — give me a few seconds.")
        return
    is_owner = app.resolver.is_owner(uid)
    user = await app.storage.get_user(uid)
    metered = not billing.is_exempt(app.plan, is_owner=is_owner, user=user)
    if metered:
        left = billing.remaining(app.plan, user)
        # translations still in flight are reserved, so a burst can't overshoot the allowance
        if left is not None and left - app.private_pending.get(uid, 0) <= 0:
            await _send_paywall(app, msg)
            return
    if not approved and not is_owner and not billing.has_paid(user) and not _daily_ok(app, uid):
        await _say(msg, "📅 Today's limit of private translations is reached. It resets at 00:00 UTC.")
        return
    if len(el.plain) > app.s.max_text_chars:
        el = Eligible(el.kind, html_escape_truncate(el.plain, app.s.max_text_chars),
                      truncate(el.plain, app.s.max_text_chars), el.has_media)
    lane = pipeline.media_lane if el.has_media else pipeline.text_lane

    async def job() -> None:
        t0 = time.perf_counter()
        with contextlib.suppress(TelegramAPIError):
            await app.bot.send_chat_action(uid, "typing")
        try:
            res = await asyncio.wait_for(pipeline.translate_private(msg, el, target),
                                         timeout=max(3.0, lane.job_timeout - 2))
        except Exception:
            log.warning("private translation failed", exc_info=True)
            res = PrivateResult(None, None, "failed")
        lang = html.escape(lang_name(target))
        if res.text:
            body = res.text  # only the translation
        else:
            body = _PRIVATE_REASONS.get(res.reason, _PRIVATE_REASONS["no_text"]).format(
                lang=lang, secs=int(app.s.max_media_seconds))
        try:
            await _reply_safe(msg, body)
            pipeline.metrics.observe(f"private_ms.{el.kind.value}", (time.perf_counter() - t0) * 1000)
            if metered and res.text:  # only delivered translations count
                await app.storage.add_private_use(uid)
                after = billing.remaining(app.plan, await app.storage.get_user(uid))
                if after in (100, 10):
                    await _say(msg, f"🎟 {billing.fmt(after)} private translations left · /buy")
        finally:
            _release(app, uid)

    if metered:
        app.private_pending[uid] = app.private_pending.get(uid, 0) + 1
    if not await lane.submit(uid, job, key=f"p:{uid}:{msg.message_id}"):
        if metered:
            _release(app, uid)
        await _say(msg, "⏳ I'm busy right now — please try again in a moment.")


def _release(app: App, uid: int) -> None:
    n = app.private_pending.get(uid, 0) - 1
    if n > 0:
        app.private_pending[uid] = n
    else:
        app.private_pending.pop(uid, None)


async def _send_paywall(app: App, msg: Message) -> None:
    with contextlib.suppress(TelegramAPIError):
        await msg.answer(billing.paywall_text(app.plan), reply_markup=ui.paywall_kb(app.plan))


async def _send_invoice(app: App, uid: int, product: str) -> str | None:
    """Telegram Stars invoice (currency XTR, no provider token). Returns an error text, or None when sent."""
    price = app.plan.price(product)
    if price is None or not app.plan.enabled:
        return "This purchase isn't available."
    if product == billing.LIFETIME:
        user = await app.storage.get_user(uid)
        if user is not None and user.lifetime:
            return "You already have lifetime access ♾"
    title, description = billing.product_title(app.plan, product)
    try:
        await app.bot.send_invoice(chat_id=uid, title=title, description=description,
                                   payload=billing.payload(product, uid, price), currency="XTR",
                                   prices=[LabeledPrice(label=title, amount=price)])
    except TelegramAPIError:
        log.warning("send_invoice failed", exc_info=True)
        return "Couldn't create the invoice — please try again."
    return None


async def _group_count(app: App, user_id: int) -> int | None:
    """Enabled groups where this user is a member and hasn't muted the bot (None if it can't be checked)."""
    try:
        chats = [c for c in await app.storage.active_chats() if c.enabled][:50]
        flags = await asyncio.gather(*(app.membership.is_member(c.chat_id, user_id) for c in chats))
        n = 0
        for c, is_member in zip(chats, flags, strict=True):
            if is_member and user_id not in await app.storage.muted_users(c.chat_id):
                n += 1
        return n
    except Exception:  # help must always render
        return None


async def _send_groups(app: App, msg: Message, user_id: int, edit: bool = False) -> None:
    rows = []
    muted = set()
    for chat in await app.storage.active_chats():
        if not chat.enabled:
            continue
        if await app.membership.is_member(chat.chat_id, user_id):
            if user_id in await app.storage.muted_users(chat.chat_id):
                muted.add(chat.chat_id)
            rows.append((chat, chat.chat_id in muted))
    text = ("<b>Your groups</b> — tap to mute/unmute translations for one group." if rows else
            "You're not in any group where the translator is active yet.")
    if edit:
        with contextlib.suppress(TelegramAPIError):
            await msg.edit_text(text, reply_markup=ui.groups_kb(rows))
    else:
        await msg.answer(text, reply_markup=ui.groups_kb(rows))


def _status_text(app: App) -> str:
    snap = app.metrics.snapshot()
    c = snap["counters"]
    assert isinstance(c, dict)
    keys = ("updates.message", "cache_hit", "cache_miss", "delivered.ephemeral", "delivered.dm_fallback",
            "fallback", "validation_fail", "skip.already_target", "media.vision", "media.no_text",
            "private.translated", "private.nothing")
    lines = ["<b>Status</b>", f"uptime {snap['uptime_s']}s · public output blocked: {app.guard.blocked}"]
    lines += [f"{k}: {c.get(k, 0)}" for k in keys]
    media = {k: v for k, v in c.items() if k.startswith(("media.too_long", "media.no_text.", "media.rejected",
                                                          "media.vision_error", "media.error"))}
    if media:
        lines.append("<b>Media skipped</b>")
        lines += [f"{html.escape(k)}: {v}" for k, v in sorted(media.items())]
    fails = {k: v for k, v in c.items() if k.startswith(("delivery_fail", "provider_error"))}
    if fails:
        lines.append("<b>Failures</b>")
        lines += [f"{html.escape(k)}: {v}" for k, v in sorted(fails.items())]
    lat = snap["latency_ms"]
    assert isinstance(lat, dict)
    for k in ("latency.gemini", "latency.deepl", "delivery_ms", "pipeline_text_ms"):
        if lat.get(k):
            lines.append(f"{k}: {lat[k]}")
    for k in sorted(k for k in lat if k.startswith(("pipeline_media_ms.", "private_ms."))):
        lines.append(f"{html.escape(k)}: {lat[k]}")
    if app.gemini:
        lines.append(f"gemini quota: {json.dumps(app.gemini.quota_snapshot())} · hedged: {app.gemini.hedged}")
    if app.deepl and app.deepl.char_limit:
        lines.append(f"deepl chars: {app.deepl.char_count}/{app.deepl.char_limit}")
    if app.pipeline:
        lines.append(f"queues: text {app.pipeline.text_lane.size} · media {app.pipeline.media_lane.size}")
    sold = {k: v for k, v in c.items() if k.startswith("payments.")}
    if sold:
        lines.append("stars sales since start: " + ", ".join(f"{k[9:]} × {v}" for k, v in sorted(sold.items())))
    return "\n".join(lines)


# --- callbacks (private chat keyboards only) --------------------------------------------------------


def _callback_router(app: App) -> Router:
    r = Router(name="callbacks")

    @r.callback_query()
    async def on_cb(cb: CallbackQuery) -> None:
        if cb.message is None or not isinstance(cb.message, Message) or cb.message.chat.type != "private":
            await cb.answer()
            return
        uid = cb.from_user.id
        data = cb.data or ""
        msg = cb.message
        if not await app.resolver.is_approved(uid) and (app.s.private_access != "everyone"
                                                         or data.partition(":")[0] not in ("lang", "ilang", "me",
                                                                                            "pay")):
            await cb.answer("This translator is private.", show_alert=True)
            return
        user = await app.storage.get_user(uid) or await app.storage.upsert_user(uid)
        prefix, _, rest = data.partition(":")
        if prefix in ("lang", "ilang"):
            current = (user.effective_lang if prefix == "lang" else user.inline_lang) or (
                app.s.default_target_lang if prefix == "lang" else "en")
            action, _, val = rest.partition(":")
            if action == "set":
                lang = resolve_language(val)
                if lang is None:
                    await cb.answer("Unknown language", show_alert=True)
                    return
                if prefix == "lang":
                    await app.storage.upsert_user(uid, lang=lang.code, onboarded=True, default_notice_sent=True)
                    note = f"✅ You'll receive translations in <b>{html.escape(lang.label)}</b>."
                else:
                    await app.storage.upsert_user(uid, inline_lang=lang.code)
                    note = f"✅ Inline translations will go to <b>{html.escape(lang.label)}</b>."
                with contextlib.suppress(TelegramAPIError):
                    await msg.edit_text(note)
                await cb.answer("Saved")
                return
            if action == "page":
                kb = ui.language_page(int(val) if _INT_RE.fullmatch(val or "") else 0, current, prefix)
            else:
                kb = ui.language_menu(current, prefix)
            with contextlib.suppress(TelegramAPIError):
                await msg.edit_reply_markup(reply_markup=kb)
            await cb.answer()
            return
        if prefix == "me":
            action, _, val = rest.partition(":")
            if action == "pause":
                user = await app.storage.upsert_user(uid, paused=not user.paused)
            elif action == "dm":
                user = await app.storage.upsert_user(uid, dm_fallback=not user.dm_fallback)
            elif action == "groups":
                await _send_groups(app, msg, uid, edit=True)
                await cb.answer()
                return
            elif action == "mute" and _INT_RE.fullmatch(val):
                chat_id = int(val)
                if not await app.membership.is_member(chat_id, uid):
                    await cb.answer("You're not a member of that group.", show_alert=True)
                    return
                muted = uid in await app.storage.muted_users(chat_id)
                await app.storage.set_muted(uid, chat_id, not muted)
                await _send_groups(app, msg, uid, edit=True)
                await cb.answer("Muted" if not muted else "Unmuted")
                return
            elif action == "privacy":
                await msg.answer(ui.PRIVACY)
                await cb.answer()
                return
            elif action == "mygroups":  # from /help: list below, keep the help message
                await _send_groups(app, msg, uid)
                await cb.answer()
                return
            elif action == "show":  # from /help: open settings below, keep the help message
                await msg.answer(ui.settings_text(user, app.s.default_target_lang),
                                 reply_markup=ui.settings_kb(user, app.s.default_target_lang))
                await cb.answer()
                return
            with contextlib.suppress(TelegramAPIError):
                await msg.edit_text(ui.settings_text(user, app.s.default_target_lang),
                                    reply_markup=ui.settings_kb(user, app.s.default_target_lang))
            await cb.answer("Saved")
            return
        if prefix == "pay":
            err = await _send_invoice(app, uid, rest)
            await cb.answer(err or "", show_alert=bool(err))
            return
        if prefix == "own":
            if not app.resolver.is_owner(uid):
                await cb.answer("Owners only.", show_alert=True)
                return
            await _owner_cb(app, cb, msg, rest)
            return
        await cb.answer()

    @r.pre_checkout_query()
    async def pre_checkout(q: PreCheckoutQuery) -> None:
        """Telegram asks before charging: re-validate product, buyer and price (10 s deadline)."""
        parsed = billing.parse_payload(q.invoice_payload)
        error: str | None = None
        if not app.plan.enabled or parsed is None or q.currency != "XTR":
            error = "This purchase is no longer available."
        else:
            product, buyer, price = parsed
            if buyer != q.from_user.id or q.total_amount != price or app.plan.price(product) != price:
                error = "The price has changed — please tap /buy again."
            elif product == billing.LIFETIME:
                user = await app.storage.get_user(buyer)
                if user is not None and user.lifetime:
                    error = "You already have lifetime access."
        with contextlib.suppress(TelegramAPIError):
            await q.answer(ok=error is None, error_message=error)

    return r


async def _owner_cb(app: App, cb: CallbackQuery, msg: Message, rest: str) -> None:
    parts = rest.split(":")
    action = parts[0]
    if action == "chats":
        with contextlib.suppress(TelegramAPIError):
            await msg.edit_reply_markup(reply_markup=ui.chats_kb(await app.storage.active_chats()))
    elif action in ("chat", "tog", "check") and len(parts) >= 2 and _INT_RE.fullmatch(parts[1]):
        chat_id = int(parts[1])
        chat = await app.storage.get_chat(chat_id)
        if chat is None:
            await cb.answer("Unknown group", show_alert=True)
            return
        if action == "tog" and len(parts) == 3:
            key = parts[2]
            if key == "enabled":
                chat = await app.storage.upsert_chat(chat_id, enabled=not chat.enabled)
            elif key in dict(ui.CHAT_TOGGLES):
                settings = dict(chat.settings)
                settings[key] = not bool(settings.get(key))
                chat = await app.storage.upsert_chat(chat_id, settings=settings)
            if app.pipeline:
                app.pipeline.invalidate_chat(chat_id)
        if action == "check":
            try:
                report = await app.verify_setup(chat_id)
                await msg.answer(report.render())
            except TelegramAPIError:
                await msg.answer("Could not check this group (is the bot still a member?).")
            chat = await app.storage.get_chat(chat_id) or chat
        with contextlib.suppress(TelegramAPIError):
            await msg.edit_text(f"<b>{html.escape(chat.title or str(chat_id))}</b> — settings",
                                reply_markup=ui.chat_settings_kb(chat))
    elif action == "recipients":
        with contextlib.suppress(TelegramAPIError):
            await msg.edit_reply_markup(reply_markup=ui.recipients_kb(await app.storage.approved_users()))
    elif action == "revoke" and len(parts) == 2 and parts[1].isdigit():
        await app.storage.set_approved(int(parts[1]), False, by=cb.from_user.id)
        with contextlib.suppress(TelegramAPIError):
            await msg.edit_reply_markup(reply_markup=ui.recipients_kb(await app.storage.approved_users()))
        await cb.answer("Revoked")
        return
    await cb.answer()


# --- inline ----------------------------------------------------------------------------------------


def _inline_router(app: App) -> Router:
    r = Router(name="inline")
    latest: dict[int, str] = {}

    @r.inline_query()
    async def inline(q: InlineQuery) -> None:
        uid = q.from_user.id
        if app.s.inline_access == "approved" and not await app.resolver.is_approved(uid):
            await q.answer([], cache_time=60, is_personal=True, button=InlineQueryResultsButton(
                text="🔒 Private translator — tap for info", start_parameter="inline"))
            return
        query = normalize_for_key(q.query or "")
        prof = profile_text(query)
        if not query or prof.kind in (ContentKind.EMPTY, ContentKind.EMOJI_ONLY, ContentKind.URL_ONLY,
                                      ContentKind.NUMERIC, ContentKind.MENTION_ONLY):
            await q.answer([], cache_time=5, is_personal=True, button=InlineQueryResultsButton(
                text="Type text to translate it", start_parameter="inline"))
            return
        # debounce: per-keystroke queries — only the newest one reaches a provider
        latest[uid] = q.id
        if len(latest) > 10_000:
            latest.pop(next(iter(latest)))
        await asyncio.sleep(0.35)
        if latest.get(uid) != q.id:
            return
        if not app.inline_limiter.try_acquire(uid):
            await q.answer([], cache_time=5, is_personal=True, button=InlineQueryResultsButton(
                text="Slow down a little — try again in a moment", start_parameter="inline"))
            return
        user = await app.storage.get_user(uid)
        target = (user.inline_lang if user else None) or "en"
        assert app.detector is not None and app.orchestrator is not None
        det = await asyncio.to_thread(app.detector.detect, query, target)
        rid = hashlib.sha256(f"{target}:{query}".encode()).hexdigest()[:32]
        if det.verdict is Verdict.TARGET:
            await q.answer([InlineQueryResultArticle(
                id=f"same{rid}", title=f"Already in {lang_name(target)}", description=truncate(query, 120),
                input_message_content=InputTextMessageContent(message_text=query, parse_mode=None))],
                cache_time=300, is_personal=True)
            return
        job = TranslationJob(text=query, target=target, source_hint=det.lang if det.verdict is Verdict.OTHER else None,
                             romanized=det.romanized, mixed=det.mixed, unsure=det.verdict is Verdict.UNSURE)
        try:
            res = await app.orchestrator.translate(job, det, purpose="inline", deadline_s=app.s.inline_timeout_s)
        except Exception:
            log.exception("inline translation failed")
            res = None
        if latest.get(uid) != q.id:
            return  # superseded while translating
        if res is None:
            await q.answer([], cache_time=5, is_personal=True, button=InlineQueryResultsButton(
                text="Translation unavailable right now — try again", start_parameter="inline"))
            return
        src = res.source_lang or det.lang
        pair = f"{lang_name(src)} → {lang_name(target)}" if src else lang_name(target)
        translation = truncate(res.translation, 4096)
        both = truncate(f"{res.translation}\n\n— {query}", 4096)
        results: list[Any] = [
            InlineQueryResultArticle(
                id=f"t{rid}", title=truncate(translation, 120), description=f"{pair} · tap to send",
                input_message_content=InputTextMessageContent(message_text=translation, parse_mode=None)),
            InlineQueryResultArticle(
                id=f"b{rid}", title="Translation + original", description=truncate(both.replace("\n", " "), 120),
                input_message_content=InputTextMessageContent(message_text=both, parse_mode=None)),
        ]
        with contextlib.suppress(TelegramAPIError):
            await q.answer(results, cache_time=300, is_personal=True)
        app.metrics.inc("inline.answered")
        if latest.get(uid) == q.id:
            del latest[uid]  # bounded: only users with a query in flight are tracked

    return r
