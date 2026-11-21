"""Online SQLite backup, safe while the bot is running (WAL mode): `python -m translator_bot.backup [DIR]`.

Writes DIR/translator-YYYYmmdd-HHMMSS.sqlite3 (default DIR: <database dir>/backups) using SQLite's backup API,
verifies it with `PRAGMA integrity_check`, and keeps the newest BACKUP_KEEP files (default 14).
The database holds settings, approvals, language choices and IDs/hashes only — never message text.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path


def backup(db_path: Path, dest_dir: Path, keep: int = 14) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"translator-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if ok != "ok":
        out.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity check: {ok}")
    old = sorted(dest_dir.glob("translator-*.sqlite3"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink(missing_ok=True)
    return out


def main() -> None:
    db = Path(os.environ.get("DATABASE_PATH", "data/translator.sqlite3"))
    if not db.exists():
        print(f"database not found: {db}", file=sys.stderr)
        raise SystemExit(2)
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else db.parent / "backups"
    out = backup(db, dest, keep=int(os.environ.get("BACKUP_KEEP", "14")))
    print(f"backup written: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
