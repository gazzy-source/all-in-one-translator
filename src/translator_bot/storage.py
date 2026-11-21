"""SQLite persistence (aiosqlite, WAL). All dynamic SQL interpolates only allow-listed column names; values are
always bound parameters (the `nosec B608` markers below).
 Stores preferences, approvals, chat config and *IDs/hashes* of messages.

Message text and translations are NOT persisted. The only persisted text is OCR output for media keyed by
Telegram `file_unique_id` (stickers/GIFs are shared assets), with a TTL.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        lang TEXT,
        onboarded INTEGER NOT NULL DEFAULT 0,
        approved INTEGER NOT NULL DEFAULT 0,
        approved_by INTEGER,
        approved_at INTEGER,
        paused INTEGER NOT NULL DEFAULT 0,
        started INTEGER NOT NULL DEFAULT 0,
        dm_fallback INTEGER NOT NULL DEFAULT 0,
        inline_lang TEXT,
        default_notice_sent INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        type TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1,
        bot_is_admin INTEGER NOT NULL DEFAULT 0,
        settings TEXT NOT NULL DEFAULT '{}',
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE topics (
        chat_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (chat_id, thread_id)
    );
    CREATE TABLE user_chat (
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        muted INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    );
    CREATE TABLE messages (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        thread_id INTEGER,
        sender_id INTEGER,
        source_lang TEXT,
        content_hash TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id)
    );
    CREATE INDEX messages_created ON messages(created_at);
    CREATE TABLE deliveries (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        recipient_id INTEGER NOT NULL,
        channel TEXT NOT NULL,
        ephemeral_id INTEGER,
        dm_message_id INTEGER,
        lang TEXT,
        status TEXT NOT NULL,
        error TEXT,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (chat_id, message_id, recipient_id)
    );
    CREATE INDEX deliveries_created ON deliveries(created_at);
    CREATE TABLE media_cache (
        file_unique_id TEXT PRIMARY KEY,
        has_text INTEGER NOT NULL,
        text TEXT,
        source_lang TEXT,
        confidence REAL,
        translations TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL
    );
    CREATE TABLE glossary (
        chat_id INTEGER NOT NULL,
        term TEXT NOT NULL,
        rendering TEXT NOT NULL,
        PRIMARY KEY (chat_id, term)
    );
    """,
]

DEFAULT_CHAT_SETTINGS: dict[str, Any] = {
    "text": True,
    "captions": True,
    "images": True,
    "stickers": True,
    "animated": True,  # TGS + video stickers
    "gifs": True,
    "videos": True,  # videos & video notes up to MAX_MEDIA_SECONDS: speech + on-screen text
    "voice": True,  # voice messages & audio up to MAX_MEDIA_SECONDS
    "show_sender": False,  # include the sender's name in the private translation header
    "context": True,
    "show_original": False,  # include a short quote of the original in the ephemeral message
    "translate_bots": False,
}


@dataclass(slots=True)
class User:
    user_id: int
    first_name: str | None = None
    lang: str | None = None
    onboarded: bool = False
    approved: bool = False
    paused: bool = False
    started: bool = False
    dm_fallback: bool = False
    inline_lang: str | None = None
    default_notice_sent: bool = False


@dataclass(slots=True)
class Chat:
    chat_id: int
    title: str | None = None
    type: str | None = None
    active: bool = True
    enabled: bool = True
    bot_is_admin: bool = False
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CHAT_SETTINGS))


def _now() -> int:
    return int(time.time())


class Storage:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("storage not opened")
        return self._db

    async def open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self.migrate()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def migrate(self) -> int:
        await self.db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        cur = await self.db.execute("SELECT MAX(version) FROM schema_version")
        row = await cur.fetchone()
        current = int(row[0] or 0) if row else 0
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= current:
                continue
            # migration SQL is a compile-time constant from MIGRATIONS; `i` is an int
            await self.db.executescript(f"BEGIN; {sql} INSERT INTO schema_version(version) VALUES ({int(i)}); COMMIT;")  # noqa: S608  # nosec B608
            current = i
        return current

    # -- users -----------------------------------------------------------------------------------
    @staticmethod
    def _user(row: aiosqlite.Row) -> User:
        return User(
            user_id=row["user_id"], first_name=row["first_name"], lang=row["lang"], onboarded=bool(row["onboarded"]),
            approved=bool(row["approved"]), paused=bool(row["paused"]), started=bool(row["started"]),
            dm_fallback=bool(row["dm_fallback"]), inline_lang=row["inline_lang"],
            default_notice_sent=bool(row["default_notice_sent"]),
        )

    async def get_user(self, user_id: int) -> User | None:
        cur = await self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return self._user(row) if row else None

    async def upsert_user(self, user_id: int, **fields: Any) -> User:
        allowed = {"first_name", "lang", "onboarded", "approved", "approved_by", "approved_at", "paused", "started",
                   "dm_fallback", "inline_lang", "default_notice_sent"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown user fields: {bad}")
        await self.db.execute("INSERT OR IGNORE INTO users(user_id, updated_at) VALUES (?, ?)", (user_id, _now()))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)  # keys validated against allow-list above
            vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
            await self.db.execute(f"UPDATE users SET {cols}, updated_at=? WHERE user_id=?",  # noqa: S608  # nosec B608
                                  (*vals, _now(), user_id))
        await self.db.commit()
        user = await self.get_user(user_id)
        assert user is not None
        return user

    async def set_approved(self, user_id: int, approved: bool, by: int) -> User:
        return await self.upsert_user(user_id, approved=approved, approved_by=by if approved else None,
                                      approved_at=_now() if approved else None)

    async def approved_users(self) -> list[User]:
        cur = await self.db.execute("SELECT * FROM users WHERE approved=1 ORDER BY user_id")
        return [self._user(r) for r in await cur.fetchall()]

    async def delete_user_data(self, user_id: int) -> None:
        """/forgetme: removes preferences and delivery records. Approval (owner decision) is kept only as a flag."""
        cur = await self.db.execute("SELECT approved FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        approved = bool(row["approved"]) if row else False
        await self.db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        await self.db.execute("DELETE FROM user_chat WHERE user_id=?", (user_id,))
        await self.db.execute("DELETE FROM deliveries WHERE recipient_id=?", (user_id,))
        await self.db.execute("UPDATE messages SET sender_id=NULL WHERE sender_id=?", (user_id,))
        if approved:
            await self.db.execute("INSERT INTO users(user_id, approved, updated_at) VALUES (?, 1, ?)",
                                  (user_id, _now()))
        await self.db.commit()

    # -- chats -----------------------------------------------------------------------------------
    @staticmethod
    def _chat(row: aiosqlite.Row) -> Chat:
        settings = dict(DEFAULT_CHAT_SETTINGS)
        with contextlib.suppress(json.JSONDecodeError):
            settings.update(json.loads(row["settings"] or "{}"))
        return Chat(chat_id=row["chat_id"], title=row["title"], type=row["type"], active=bool(row["active"]),
                    enabled=bool(row["enabled"]), bot_is_admin=bool(row["bot_is_admin"]), settings=settings)

    async def get_chat(self, chat_id: int) -> Chat | None:
        cur = await self.db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return self._chat(row) if row else None

    async def upsert_chat(self, chat_id: int, **fields: Any) -> Chat:
        allowed = {"title", "type", "active", "enabled", "bot_is_admin", "settings"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown chat fields: {bad}")
        await self.db.execute("INSERT OR IGNORE INTO chats(chat_id, updated_at) VALUES (?, ?)", (chat_id, _now()))
        if "settings" in fields:
            fields["settings"] = json.dumps(fields["settings"], sort_keys=True)
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            vals = [int(v) if isinstance(v, bool) else v for v in fields.values()]
            await self.db.execute(f"UPDATE chats SET {cols}, updated_at=? WHERE chat_id=?",  # noqa: S608  # nosec B608
                                  (*vals, _now(), chat_id))
        await self.db.commit()
        chat = await self.get_chat(chat_id)
        assert chat is not None
        return chat

    async def active_chats(self) -> list[Chat]:
        cur = await self.db.execute("SELECT * FROM chats WHERE active=1 ORDER BY title")
        return [self._chat(r) for r in await cur.fetchall()]

    async def migrate_chat_id(self, old: int, new: int) -> None:
        """Group upgraded to supergroup: chat id changes.

        The new id's row may already exist (its my_chat_member update can arrive before migrate_to_chat_id, and
        is created disabled with default settings): the old row's enabled flag and settings win, then the old
        row is removed. `UPDATE OR IGNORE` alone silently kept the old row and lost the configuration."""
        cur = await self.db.execute("SELECT enabled, settings, title FROM chats WHERE chat_id=?", (old,))
        old_row = await cur.fetchone()
        cur = await self.db.execute("SELECT 1 FROM chats WHERE chat_id=?", (new,))
        if old_row is not None and await cur.fetchone() is not None:
            await self.db.execute(
                "UPDATE chats SET enabled=?, settings=?, title=COALESCE(title, ?), active=1, updated_at=? "
                "WHERE chat_id=?", (old_row["enabled"], old_row["settings"], old_row["title"], _now(), new))
        for table in ("chats", "topics", "user_chat", "messages", "deliveries", "glossary"):
            await self.db.execute(f"UPDATE OR IGNORE {table} SET chat_id=? WHERE chat_id=?", (new, old))  # noqa: S608  # nosec B608
            await self.db.execute(f"DELETE FROM {table} WHERE chat_id=?", (old,))  # noqa: S608  # nosec B608
        await self.db.commit()

    async def topic_enabled(self, chat_id: int, thread_id: int | None) -> bool:
        if thread_id is None:
            return True
        cur = await self.db.execute("SELECT enabled FROM topics WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))
        row = await cur.fetchone()
        return bool(row["enabled"]) if row else True

    async def set_topic_enabled(self, chat_id: int, thread_id: int, enabled: bool) -> None:
        await self.db.execute(
            "INSERT INTO topics(chat_id, thread_id, enabled) VALUES (?,?,?) "
            "ON CONFLICT(chat_id, thread_id) DO UPDATE SET enabled=excluded.enabled", (chat_id, thread_id, int(enabled)))
        await self.db.commit()

    # -- per-user per-chat -----------------------------------------------------------------------
    async def muted_users(self, chat_id: int) -> set[int]:
        cur = await self.db.execute("SELECT user_id FROM user_chat WHERE chat_id=? AND muted=1", (chat_id,))
        return {r["user_id"] for r in await cur.fetchall()}

    async def set_muted(self, user_id: int, chat_id: int, muted: bool) -> None:
        await self.db.execute(
            "INSERT INTO user_chat(user_id, chat_id, muted) VALUES (?,?,?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET muted=excluded.muted", (user_id, chat_id, int(muted)))
        await self.db.commit()

    # -- message claims (idempotency) ------------------------------------------------------------
    async def claim_message(self, chat_id: int, message_id: int, thread_id: int | None, sender_id: int | None,
                            content_hash: str) -> bool:
        """Atomically claim a message for processing. False if already claimed (duplicate update)."""
        cur = await self.db.execute(
            "INSERT OR IGNORE INTO messages(chat_id, message_id, thread_id, sender_id, content_hash, status, created_at) "
            "VALUES (?,?,?,?,?, 'processing', ?)", (chat_id, message_id, thread_id, sender_id, content_hash, _now()))
        await self.db.commit()
        return cur.rowcount == 1

    async def get_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_message(self, chat_id: int, message_id: int, **fields: Any) -> None:
        allowed = {"status", "source_lang", "content_hash"}
        if set(fields) - allowed:
            raise ValueError("bad message fields")
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(f"UPDATE messages SET {cols} WHERE chat_id=? AND message_id=?",  # noqa: S608  # nosec B608
                              (*fields.values(), chat_id, message_id))
        await self.db.commit()

    # -- deliveries ------------------------------------------------------------------------------
    async def record_delivery(self, chat_id: int, message_id: int, recipient_id: int, *, channel: str, status: str,
                              lang: str | None, ephemeral_id: int | None = None, dm_message_id: int | None = None,
                              error: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO deliveries(chat_id, message_id, recipient_id, channel, ephemeral_id, dm_message_id, lang, status,"
            " error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id, message_id, recipient_id) DO UPDATE SET"
            " channel=excluded.channel, ephemeral_id=COALESCE(excluded.ephemeral_id, ephemeral_id),"
            " dm_message_id=COALESCE(excluded.dm_message_id, dm_message_id), lang=excluded.lang,"
            " status=excluded.status, error=excluded.error",
            (chat_id, message_id, recipient_id, channel, ephemeral_id, dm_message_id, lang, status, error, _now()))
        await self.db.commit()

    async def ping(self) -> None:
        cur = await self.db.execute("SELECT 1")
        await cur.fetchone()

    async def messages_for_ephemeral(self, chat_id: int, ephemeral_id: int) -> list[int]:
        """Original message ids whose translation lives in this ephemeral (a burst covers several)."""
        cur = await self.db.execute("SELECT DISTINCT message_id FROM deliveries WHERE chat_id=? AND ephemeral_id=?",
                                    (chat_id, ephemeral_id))
        return [int(r["message_id"]) for r in await cur.fetchall()]

    async def deliveries_for(self, chat_id: int, message_id: int) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM deliveries WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        return [dict(r) for r in await cur.fetchall()]

    async def delivery_stats(self, since_s: int = 86400) -> dict[str, int]:
        cur = await self.db.execute(
            "SELECT channel || ':' || status AS k, COUNT(*) AS n FROM deliveries WHERE created_at>=? GROUP BY k",
            (_now() - since_s,))
        return {r["k"]: r["n"] for r in await cur.fetchall()}

    # -- media OCR cache -------------------------------------------------------------------------
    async def get_media(self, file_unique_id: str, ttl_s: int) -> dict[str, Any] | None:
        cur = await self.db.execute("SELECT * FROM media_cache WHERE file_unique_id=? AND created_at>=?",
                                    (file_unique_id, _now() - ttl_s))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["translations"] = json.loads(d["translations"] or "{}")
        return d

    async def put_media(self, file_unique_id: str, *, has_text: bool, text: str | None, source_lang: str | None,
                        confidence: float, translations: dict[str, str]) -> None:
        await self.db.execute(
            "INSERT INTO media_cache(file_unique_id, has_text, text, source_lang, confidence, translations, created_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(file_unique_id) DO UPDATE SET has_text=excluded.has_text,"
            " text=excluded.text, source_lang=excluded.source_lang, confidence=excluded.confidence,"
            " translations=excluded.translations",
            (file_unique_id, int(has_text), text, source_lang, confidence, json.dumps(translations, ensure_ascii=False),
             _now()))
        await self.db.commit()

    # -- glossary --------------------------------------------------------------------------------
    async def glossary(self, chat_id: int) -> list[tuple[str, str]]:
        cur = await self.db.execute("SELECT term, rendering FROM glossary WHERE chat_id=? ORDER BY term LIMIT 100",
                                    (chat_id,))
        return [(r["term"], r["rendering"]) for r in await cur.fetchall()]

    async def set_glossary(self, chat_id: int, term: str, rendering: str | None) -> None:
        if rendering is None:
            await self.db.execute("DELETE FROM glossary WHERE chat_id=? AND term=?", (chat_id, term))
        else:
            await self.db.execute("INSERT INTO glossary(chat_id, term, rendering) VALUES (?,?,?) ON CONFLICT(chat_id, term)"
                                  " DO UPDATE SET rendering=excluded.rendering", (chat_id, term, rendering))
        await self.db.commit()

    # -- retention -------------------------------------------------------------------------------
    async def cleanup(self, message_retention_s: int, media_ttl_s: int) -> dict[str, int]:
        cutoff = _now() - message_retention_s
        c1 = await self.db.execute("DELETE FROM messages WHERE created_at<?", (cutoff,))
        c2 = await self.db.execute("DELETE FROM deliveries WHERE created_at<?", (cutoff,))
        c3 = await self.db.execute("DELETE FROM media_cache WHERE created_at<?", (_now() - media_ttl_s,))
        await self.db.commit()
        return {"messages": c1.rowcount, "deliveries": c2.rowcount, "media": c3.rowcount}
