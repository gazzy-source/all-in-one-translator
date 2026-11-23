"""Texts and inline keyboards for private-chat UX. All group interaction is ephemeral and minimal."""

from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from translator_bot.billing import Plan
from translator_bot.languages import LANGUAGES, POPULAR, Language, lang_name
from translator_bot.storage import DEFAULT_CHAT_SETTINGS, Chat, User

PAGE = 18


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def lang_button(lang: Language, current: str | None, prefix: str) -> InlineKeyboardButton:
    mark = "✅ " if lang.code == current else flag(lang.code) + " "
    return _btn(f"{mark}{lang.native}", f"{prefix}:set:{lang.code}")


def language_menu(current: str | None, prefix: str = "lang") -> InlineKeyboardMarkup:
    codes = [c for c in POPULAR if c in LANGUAGES]
    if current in LANGUAGES and current not in codes:
        codes.insert(0, current)  # e.g. Burmese: the reader's own language is always on the first page
    pop = [lang_button(LANGUAGES[c], current, prefix) for c in codes]
    rows = _rows(pop, 3)
    rows.append([_btn("🌍 More languages A–Z ›", f"{prefix}:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_page(page: int, current: str | None, prefix: str = "lang") -> InlineKeyboardMarkup:
    # the languages not on the first page, A–Z (the first page lists the most used ones)
    langs = sorted((lg for lg in LANGUAGES.values() if lg.code not in POPULAR), key=lambda lang: lang.name)
    pages = max(1, (len(langs) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = langs[page * PAGE:(page + 1) * PAGE]
    rows = _rows([_btn(("✅ " if lg.code == current else flag(lg.code) + " ") + lg.name, f"{prefix}:set:{lg.code}")
                  for lg in chunk], 3)
    nav = []
    if page > 0:
        nav.append(_btn("‹ Prev", f"{prefix}:page:{page - 1}"))
    nav.append(_btn(f"{page + 1}/{pages}", f"{prefix}:menu"))
    if page < pages - 1:
        nav.append(_btn("Next ›", f"{prefix}:page:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def suggestions_kb(langs: list[Language], current: str | None, prefix: str = "lang") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*_rows([lang_button(lg, current, prefix) for lg in langs], 2), [_btn("🌍 All languages", f"{prefix}:page:0")]])


LANGUAGE_PROMPT = (
    "🌐 <b>Which language should I translate into?</b>\n"
    "Tap one below, or type its name (e.g. <i>Hindi</i>, <i>español</i>, <i>ja</i>)."
)


MEDIA_TYPES = "text, stickers, GIFs, photos, videos and voice notes"


def _how_lines(*, uid: int, bot_username: str, approved: bool, private_ok: bool, inline_ok: bool,
               max_seconds: int) -> list[str]:
    """The three ways to use the bot, one short line each (shared by /start and /help)."""
    lines = []
    if private_ok:
        lines.append(f"📩 <b>Here</b> — send or forward text, stickers, GIFs, photos or voice (≤ {max_seconds} s)")
    if approved:
        lines.append("💬 <b>Groups</b> — translations appear right in the chat, visible <b>only to you</b>")
    else:
        lines.append(f"💬 <b>Groups</b> — ask the owner to approve you · your ID <code>{uid}</code>")
    if inline_ok:
        lines.append(f"✍️ <b>Anywhere</b> — type <code>@{html.escape(bot_username)} hello</code>, tap to send")
    return lines


def welcome(user: User, *, default_lang: str, is_owner: bool, bot_username: str = "this_bot",
            approved: bool = True, private_ok: bool = True, inline_ok: bool = True, max_seconds: int = 60) -> str:
    lang = html.escape(lang_name(user.effective_lang or default_lang))
    name = html.escape((user.first_name or "").strip()[:40])
    lines = [f"👋 <b>Hi{' ' + name if name else ''}! I translate into {lang}.</b>", ""]
    lines += _how_lines(uid=user.user_id, bot_username=bot_username, approved=approved, private_ok=private_ok,
                        inline_ok=inline_ok, max_seconds=max_seconds)
    lines.append("")
    if not user.lang:
        lines.append("<i>Picked automatically — change it below.</i>" if user.effective_lang
                     else f"<i>{html.escape(lang_name(default_lang))} is selected by default — choose yours below.</i>")
    elif user.paused and approved:
        lines.append("⏸ Group translations are paused — /resume")
    lines.append("/help · /language" + (" · 👑 /status" if is_owner else ""))
    return "\n".join(lines)


ACCESS_DENIED = (
    "🔒 This translator only works for people approved by its owner.\n"
    "Send the owner your Telegram ID: <code>{uid}</code>"
)


def flag(code: str | None) -> str:
    """Emoji flag of a language (decorative; 🌐 where no flag exists, e.g. Catalan, Esperanto)."""
    lg = LANGUAGES.get(code or "")
    return (lg.flag if lg and lg.flag else "🌐") if code else "🌐"


def lang_source(user: User | None) -> str:
    """Where the reader's language comes from, in a few words."""
    if user is None or user.lang:
        return "your choice" if user is not None and user.lang else "default"
    if user.lang_guess:
        return "auto · from how you write"
    if user.tg_lang:
        return "auto · from your Telegram app"
    return "default"


def help_text(*, uid: int, lang: str, bot_username: str, approved: bool, private_ok: bool, inline_ok: bool,
              max_seconds: int = 60, is_owner: bool = False, paused: bool = False, user: User | None = None,
              groups: int | None = None, plan_line: str | None = None) -> str:
    """/help: who I am for you right now, the three ways to use me, then collapsible help and commands."""
    status = f"{flag(lang)} <b>{html.escape(lang_name(lang))}</b> <i>({lang_source(user)})</i>"
    if paused and approved:
        status += " · ⏸ paused"
    lines = [status]
    if approved and groups is not None:
        lines.append(f"💬 Translating for you in <b>{groups}</b> group{'' if groups == 1 else 's'}" if groups
                     else "💬 Not in a translated group yet")
    if plan_line:
        lines.append(plan_line + " · /buy")
    lines.append("")
    lines += _how_lines(uid=uid, bot_username=bot_username, approved=approved, private_ok=private_ok,
                        inline_ok=inline_ok, max_seconds=max_seconds)
    tips = ["Your own messages and ones already in your language are skipped.",
            "The bot must be an admin in the group (no rights needed)."]
    tips.append("Check you haven't muted that group: /groups" if approved
                else f"Ask the owner to approve you · your ID <code>{uid}</code>")
    tips.append("Stickers/GIFs need readable text; voice and video up to " + str(max_seconds) + " s.")
    cmds = ["/language — change your language", "/settings — preferences"]
    if approved:
        cmds += ["/groups — mute me in one group", "/pause · /resume — group translations"]
    if plan_line:
        cmds.append("/buy — more private translations · /paysupport — payment help")
    cmds.append("/privacy · /forgetme — your data")
    if is_owner:
        cmds.append("👑 /recipients · /chats · /status · /approvefree · /refund")
    nl = chr(10)
    lines += ["", "<blockquote expandable>❓ <b>Not getting a translation?</b>" + nl
              + nl.join("• " + t for t in tips) + "</blockquote>",
              "<blockquote expandable>⌨️ <b>All commands</b>" + nl + nl.join(cmds) + "</blockquote>"]
    return nl.join(lines)


def help_kb(lang: str | None = None, *, approved: bool = True) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{flag(lang)} Change language", "lang:menu"), _btn("⚙️ Settings", "me:show")]]
    rows.append([_btn("👥 My groups", "me:mygroups"), _btn("🔒 Privacy", "me:privacy")] if approved
                else [_btn("🔒 Privacy", "me:privacy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def paywall_kb(plan: Plan) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"⭐ {plan.pack_price:,} · {plan.pack_size:,} more translations", "pay:pack")],
        [_btn(f"♾ {plan.lifetime_price:,} · Unlimited forever", "pay:life")],
    ])


PRIVACY = (
    "<b>Privacy</b>\n"
    "• To translate, the bot receives group messages (as an admin) and what you send it here. Message text "
    "is processed in memory, never written to disk, and never logged.\n"
    "• Text that needs translating is sent to the translation services (DeepL, Google Gemini); their privacy "
    "terms apply to what they receive.\n"
    "• Media (stickers, GIFs, photos, voice notes, videos) is downloaded temporarily, processed and "
    "deleted immediately. Text recognized in stickers/GIFs is cached by Telegram file id for reuse.\n"
    "• Stored: your user ID, chosen languages, pause/mute settings, approval status, and message/delivery "
    "IDs (no content) for up to 48 h to handle edits and avoid duplicates.\n"
    "• Translations are shown only to you via Telegram ephemeral messages. Telegram may not deliver them when "
    "you're offline and may remove them later; this is not end-to-end encryption.\n"
    "• Purchases: Telegram processes Stars payments; the bot keeps the payment ID, amount and what it unlocked "
    "(needed for refunds) and a count of your private-chat translations.\n"
    "/forgetme deletes your preferences and delivery records (purchases and usage counts are kept)."
)


def settings_kb(user: User, default_lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"🌐 Language: {lang_name(user.effective_lang or default_lang)}", "lang:menu")],
        [_btn("▶️ Resume translations" if user.paused else "⏸ Pause translations", "me:pause")],
        [_btn(("✅" if user.dm_fallback else "⬜") + " Private-chat backup if in-group delivery fails",
              "me:dm")],
        [_btn(f"✍️ Inline target: {lang_name(user.inline_lang or 'en')}", "ilang:menu")],
        [_btn("👥 My groups", "me:groups"), _btn("🔒 Privacy", "me:privacy")],
    ])


def settings_text(user: User, default_lang: str) -> str:
    return (
        "<b>Your settings</b>\n"
        f"Language: <b>{html.escape(lang_name(user.effective_lang or default_lang))}</b>"
        f"{' (default)' if not user.lang else ''}\n"
        f"Translations: {'⏸ paused' if user.paused else '▶️ active'}\n"
        f"Private-chat backup: {'on' if user.dm_fallback else 'off'} — if a private in-group translation can't "
        "be delivered, send it to this chat instead (never to the group).\n"
        f"Inline translator target: {html.escape(lang_name(user.inline_lang or 'en'))}"
    )


def groups_kb(groups: list[tuple[Chat, bool]]) -> InlineKeyboardMarkup:
    rows = [[_btn(("🔕 " if muted else "🔔 ") + (c.title or str(c.chat_id))[:40], f"me:mute:{c.chat_id}")]
            for c, muted in groups]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[_btn("↻ Refresh", "me:groups")]])


# --- owner ---------------------------------------------------------------------------------------

CHAT_TOGGLES = [("enabled", "Translation"), ("text", "Text"), ("captions", "Captions"), ("images", "Images (OCR)"),
                ("stickers", "Stickers"), ("animated", "Animated/video stickers"), ("gifs", "GIFs"),
                ("videos", "Videos (speech + text, ≤1 min)"), ("voice", "Voice messages (≤1 min)"),
                ("show_sender", "Show sender name"),
                ("context", "Conversation context"), ("show_original", "Quote original"),
                ("translate_bots", "Translate other bots")]


def chats_kb(chats: list[Chat]) -> InlineKeyboardMarkup:
    rows = [[_btn(("🟢 " if c.enabled and c.bot_is_admin else "🟡 " if c.enabled else "⚪ ")
                  + (c.title or str(c.chat_id))[:40], f"own:chat:{c.chat_id}")] for c in chats]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[_btn("↻ Refresh", "own:chats")]])


def chat_settings_kb(chat: Chat) -> InlineKeyboardMarkup:
    rows = []
    for key, label in CHAT_TOGGLES:
        on = chat.enabled if key == "enabled" else bool(chat.settings.get(key, DEFAULT_CHAT_SETTINGS.get(key)))
        rows.append([_btn(("✅ " if on else "⬜ ") + label, f"own:tog:{chat.chat_id}:{key}")])
    rows.append([_btn("🔍 Re-check setup", f"own:check:{chat.chat_id}"), _btn("‹ Back", "own:chats")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recipients_kb(users: list[User]) -> InlineKeyboardMarkup:
    rows = [[_btn(f"❌ {html.escape(u.first_name or '')[:24]} ({u.user_id}) · {lang_name(u.effective_lang or 'en')}",
                  f"own:revoke:{u.user_id}")] for u in users]
    return InlineKeyboardMarkup(inline_keyboard=rows or [[_btn("↻ Refresh", "own:recipients")]])
