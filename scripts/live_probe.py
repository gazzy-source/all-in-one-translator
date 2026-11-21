"""LIVE Telegram probe (real bot token from .env). Run while the bot itself is NOT running.

    python scripts/live_probe.py updates                       # show pending updates: user ids, group ids
    python scripts/live_probe.py check  <group_id>              # bot admin status, privacy, members we know
    python scripts/live_probe.py ephem  <group_id> <user_id> [reply_to_message_id]
    python scripts/live_probe.py errors <group_id> <member_id>  # records real error texts for edge cases

Every send goes through PublicOutputGuard, so the probe itself cannot post publicly.
Nothing here deletes or edits members' messages.
"""

from __future__ import annotations

import asyncio
import json
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import EphemeralMessageParameters, ReplyParameters

from translator_bot.config import load_settings
from translator_bot.guard import PublicOutputGuard


def out(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def main() -> None:
    s = load_settings_lenient()
    bot = Bot(s)
    bot.session.middleware(PublicOutputGuard())
    cmd = sys.argv[1] if len(sys.argv) > 1 else "updates"
    try:
        if cmd == "updates":
            ups = await bot.get_updates(timeout=1, allowed_updates=["message", "my_chat_member", "chat_member"])
            seen = []
            for u in ups:
                m = u.message or (u.my_chat_member and u.my_chat_member)
                if u.message:
                    seen.append({"update": u.update_id, "chat": u.message.chat.id, "chat_type": u.message.chat.type,
                                 "chat_title": u.message.chat.title, "from_id": u.message.from_user.id
                                 if u.message.from_user else None,
                                 "from_name": u.message.from_user.full_name if u.message.from_user else None,
                                 "text_len": len(u.message.text or "")})
                elif u.my_chat_member:
                    seen.append({"update": u.update_id, "my_chat_member": u.my_chat_member.chat.id,
                                 "title": u.my_chat_member.chat.title, "status": u.my_chat_member.new_chat_member.status,
                                 "by": u.my_chat_member.from_user.id})
                del m
            out(seen)
        elif cmd == "check":
            gid = int(sys.argv[2])
            me = await bot.get_me()
            mem = await bot.get_chat_member(gid, me.id)
            chat = await bot.get_chat(gid)
            out({"chat": {"id": chat.id, "type": chat.type, "title": chat.title, "is_forum": chat.is_forum},
                 "bot_status": mem.status, "bot_member": mem.model_dump(exclude_none=True),
                 "privacy_mode_on": not me.can_read_all_group_messages})
        elif cmd == "ephem":
            gid, uid = int(sys.argv[2]), int(sys.argv[3])
            reply = int(sys.argv[4]) if len(sys.argv) > 4 else None
            msg = await bot.send_message(
                gid, "🌐 <b>Probe</b> <i>English</i>\nThis private test message should be visible ONLY to you.",
                parse_mode="HTML", disable_notification=True,
                ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=uid),
                reply_parameters=ReplyParameters(message_id=reply) if reply else None)
            out({"ok": True, "message_id": msg.message_id, "ephemeral_message_id": msg.ephemeral_message_id,
                 "receiver": msg.receiver_user.id if msg.receiver_user else None})
            if msg.ephemeral_message_id:
                await asyncio.sleep(2)
                await bot.edit_ephemeral_message_text(chat_id=gid, receiver_user_id=uid,
                                                      ephemeral_message_id=msg.ephemeral_message_id,
                                                      text="🌐 <b>Probe</b> <i>edited</i>\nEdited in place.",
                                                      parse_mode="HTML")
                out({"edit": "ok"})
        elif cmd == "errors":
            gid, member = int(sys.argv[2]), int(sys.argv[3])
            cases = {
                "non_member_receiver": dict(receiver=777000, reply=None),  # Telegram service account
                "missing_reply_target": dict(receiver=member, reply=2_000_000_000),
            }
            results = {}
            for name, c in cases.items():
                try:
                    await bot.send_message(gid, "probe", disable_notification=True,
                                           ephemeral_message_parameters=EphemeralMessageParameters(
                                               receiver_user_id=c["receiver"]),
                                           reply_parameters=ReplyParameters(message_id=c["reply"]) if c["reply"] else None)
                    results[name] = "SUCCEEDED (no error)"
                except TelegramAPIError as exc:
                    results[name] = f"{type(exc).__name__}: {exc}"
            out(results)
        else:
            print(__doc__)
    finally:
        await bot.session.close()


def load_settings_lenient() -> str:
    """Only the token is needed here; tolerate missing OWNER_IDS / provider keys."""
    import re
    from pathlib import Path

    try:
        return load_settings().bot_token.get_secret_value()
    except Exception:
        m = re.search(r"(?m)^BOT_TOKEN=(\S+)", Path(".env").read_text(encoding="utf-8"))
        if not m:
            raise SystemExit("BOT_TOKEN missing in .env") from None
        return m.group(1)


if __name__ == "__main__":
    asyncio.run(main())
