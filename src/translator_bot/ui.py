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
    rows.append([_btn("‹ Help & controls", "help:home")])
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
    rows.append([_btn("‹ Help & controls", "help:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def suggestions_kb(langs: list[Language], current: str | None, prefix: str = "lang") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[*_rows([lang_button(lg, current, prefix) for lg in langs], 2), [_btn("🌍 All languages", f"{prefix}:page:0")]])


LANGUAGE_PROMPT = (
    "🌐 <b>Which language should I translate into?</b>\n"
    "Tap one below, or type its name (e.g. <i>Hindi</i>, <i>español</i>, <i>ja</i>)."
)


MEDIA_TYPES = "text, stickers, GIFs, photos, videos and voice notes"


def home(user: User, default_lang: str, *, approved: bool, private_ok: bool) -> tuple[str, InlineKeyboardMarkup]:
    lang = user.effective_lang or default_lang
    lines = [f"<b>Your translator</b> · {html.escape(lang_name(lang))}"]
    if private_ok:
        lines.append("To translate, send or forward a message here.")
    if approved:
        lines.append("Automatic group translations are paused." if user.paused else
                     "Group translations appear beside the conversation.")
    else:
        lines.append("For group translations, ask a group admin to reply /approve to your message.")
    rows = [[_btn("Recent translations", "recent:menu:0")]] if approved else []
    rows += [[_btn("Change language", "lang:menu"), _btn("Settings", "me:show")],
             [_btn("Help", "help:home")]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


ACCESS_DENIED = (
    "🔒 Ask a group admin to approve you — they reply /approve to one of your messages in the group "
    "(only they see it). Your Telegram ID: <code>{uid}</code>"
)


def flag(code: str | None) -> str:
    """Emoji flag of a language (decorative; 🌐 where no flag exists, e.g. Catalan, Esperanto)."""
    lg = LANGUAGES.get(code or "")
    return (lg.flag if lg and lg.flag else "🌐") if code else "🌐"


def help_kb(lang: str | None = None, *, approved: bool = True, paused: bool = False) -> InlineKeyboardMarkup:
    rows = [[_btn(f"{flag(lang)} Change language", "lang:menu"), _btn("⚙️ Settings", "me:show")]]
    if approved:
        rows.append([_btn("▶️ Resume mine" if paused else "⏸ Pause mine",
                          "me:resume" if paused else "me:stop"), _btn("👥 My groups", "me:mygroups")])
    rows += [[_btn("📖 Read translations", "help:read"), _btn("✍️ Write & reply", "help:write")],
             [_btn("👥 Set up a group", "help:setup"), _btn("❓ Missing translations", "help:missing")],
             [_btn("🔒 Privacy", "me:privacy"), _btn("‹ Home", "home:open")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_topic(topic: str, max_seconds: int = 60) -> str | None:
    return {
        "read": (
            "📖 <b>Read in your language</b>\n\n"
            "Choose your reading language with /language. English readers receive Burmese → English; "
            "Burmese readers receive English → Burmese. Each person sees only their own translations.\n\n"
            "Open /recent to browse recent deliveries by group and topic. Use /settings → I also read to skip languages you understand. "
            "Your chosen reading language stays selected even if you write in another language.\n\n"
            "<b>Your controls</b>\n/pause stops automatic translations for you in every group. "
            "/resume turns them back on. /groups lets you mute just one group. "
            "These settings never disable translations for other members. "
            "Reply /tr to a message for a one-off translation, even while paused."
        ),
        "write": (
            "✍️ <b>Write and reply across languages</b>\n\n"
            "In a group, reply <code>/tr</code> to a message to read it in your language. "
            "Or send <code>/tr your text</code> to prepare a message in the group's languages. "
            "Only you see the result; you choose whether to copy or send it.\n\n"
            "In this private chat, send or forward a message to translate it into your reading language. "
            "Private-chat access and usage limits apply. /buy shows available plans; "
            "/paysupport is for payment help, and /terms explains purchases.\n\n"
            f"Photos and stickers need readable text. Voice and video can be up to {max_seconds} seconds."
        ),
        "setup": (
            "👥 <b>Set up a multilingual group</b>\n\n"
            "1. Add the bot and make it an admin; no extra admin rights are needed.\n"
            "2. A group admin replies <code>/approve</code> to a member. Only the group owner can use "
            "<code>/approveall</code> for members seen in this group.\n"
            "3. Each reader opens the bot privately and chooses /language.\n\n"
            "Members must still belong to the group. Each can pause, resume or mute their own translations. "
            "Group admins can use /revoke or /revokeall to withdraw their group's approvals. "
            "The bot never posts a public translation."
        ),
        "missing": (
            "❓ <b>Missing a translation?</b>\n\n"
            "• Check /settings: are your translations paused or your reading language incorrect?\n"
            "• Check /groups: is this group muted?\n"
            "• Check I also read: understood languages are skipped.\n"
            "• Use the same Telegram account that is approved and currently in the group. "
            "Ask a group admin about /approve, or the group owner about /approveall. The bot must be an admin too.\n"
            "• Your own messages and messages already in your language are skipped automatically. "
            "Song requests after /play can be translated. Other commands are skipped.\n"
            "• Telegram may miss private in-group translations while you are offline. Providers can "
            "also be temporarily unavailable. Reply /tr to the original to request it again."
        ),
    }.get(topic)


def help_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("‹ Help & controls", "help:home")]])


def paywall_kb(plan: Plan) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"⭐ {plan.pack_price:,} · {plan.pack_size:,} more translations", "pay:pack")],
        [_btn(f"♾ {plan.lifetime_price:,} · Unlimited forever", "pay:life")],
    ])


def terms_text(plan: Plan) -> str:
    return (
        "<b>Terms of purchase</b>\n"
        f"• Everyone gets {plan.free:,} free private-chat translations. After that: {plan.pack_size:,} more for "
        f"{plan.pack_price:,} ⭐, or unlimited for life for {plan.lifetime_price:,} ⭐ (Telegram Stars).\n"
        "• A translation counts only when it's delivered to you. Purchases are digital, tied to your Telegram "
        "account, and take effect immediately.\n"
        "• \"Lifetime\" means for as long as this bot is operated. Translations are made by AI services and may "
        "contain mistakes; delivery speed depends on Telegram and those services.\n"
        "• Problems or refund requests: /paysupport. Refunds are made in Stars to the same account.\n"
        "• Group translations aren't sold; they're for members approved by the owner."
    )


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
    "• Recent translations stay in memory for up to 15 minutes. Pages opened here remain in your Telegram chat.\n"
    "• Purchases: Telegram processes Stars payments; the bot keeps the payment ID, amount and what it unlocked "
    "(needed for refunds) and a count of your private-chat translations.\n"
    "/forgetme deletes your preferences and delivery records (purchases and usage counts are kept)."
)


def settings_kb(user: User, default_lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn(f"🌐 Language: {lang_name(user.effective_lang or default_lang)}", "lang:menu")],
        [_btn("▶️ Resume translations" if user.paused else "⏸ Pause translations", "me:resume" if user.paused else "me:stop")],
        [_btn(("✅" if user.dm_fallback else "⬜") + " Private-chat backup if in-group delivery fails",
              "me:dm:off" if user.dm_fallback else "me:dm:on")],
        [_btn("📚 I also read: " + (", ".join(flag(c) for c in sorted(user.also_reads_set)) or "—"), "also:menu")],
        [_btn(("✅" if user.show_original else "⬜") + " Show the original above translations", "me:orig:off" if user.show_original else "me:orig:on")],
        [_btn(f"✍️ Inline target: {lang_name(user.inline_lang or 'en')}", "ilang:menu")],
        [_btn("👥 My groups", "me:groups"), _btn("🔒 Privacy", "me:privacy")],
        [_btn("‹ Home", "home:open")],
    ])


ALSO_PROMPT = ("📚 <b>Languages you also read</b>\nAutomatic group translations skip messages confidently identified "
               "as one of these languages. Mixed or uncertain messages may still be translated. "
               "Tap to switch on/off. You can always request a translation with /tr.")


def also_reads_kb(user: User, default_lang: str, page: int = 0) -> InlineKeyboardMarkup:
    mine = user.effective_lang or default_lang
    picked = user.also_reads_set
    codes = [c for c in POPULAR if c in LANGUAGES and c != mine]
    codes += sorted((c for c in LANGUAGES if c != mine and c not in codes), key=lambda c: LANGUAGES[c].name)
    pages = max(1, (len(codes) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    buttons = [_btn(("✅ " if c in picked else flag(c) + " ") + LANGUAGES[c].native, f"also:tog:{c}:{page}")
               for c in codes[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page:
        nav.append(_btn("‹ Back", f"also:page:{page - 1}"))
    if page + 1 < pages:
        nav.append(_btn("More languages ›", f"also:page:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[*_rows(buttons, 3), *([nav] if nav else []),
                                                 [_btn("✔️ Done", "also:done")]])


def settings_text(user: User, default_lang: str) -> str:
    return "<b>Your settings</b>\nChoose what you receive. You can always reply /tr for a single translation."


def groups_kb(groups: list[tuple[Chat, bool]]) -> InlineKeyboardMarkup:
    rows = [[_btn(("🔕 " if muted else "🔔 ") + (c.title or str(c.chat_id))[:40], f"me:mute:{c.chat_id}")]
            for c, muted in groups]
    return InlineKeyboardMarkup(inline_keyboard=[*(rows or [[_btn("↻ Refresh", "me:groups")]]),
                                                [_btn("‹ Help & controls", "help:home")]])


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
