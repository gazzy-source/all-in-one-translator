"""Short-lived, recipient-scoped history of successful deliveries. Process memory only."""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Entry:
    uid: int
    chat: int
    topic: int
    mid: int
    lang: str
    title: str
    html: str
    source_hash: bytes
    created: float


class Recent:
    def __init__(self) -> None:
        self._rows: OrderedDict[tuple[int, int, int, str], Entry] = OrderedDict()

    def _expire(self) -> None:
        cutoff = time.monotonic() - 900
        while self._rows and next(iter(self._rows.values())).created < cutoff:
            self._rows.popitem(last=False)

    def put(self, uid: int, chat: int, topic: int | None, mid: int, lang: str, title: str,
            html: str, source: str | None) -> None:
        self._expire()
        key = uid, chat, mid, lang
        self._rows.pop(key, None)
        self._rows[key] = Entry(uid, chat, topic or 0, mid, lang, title, html,
                                hashlib.sha256(source.encode()).digest() if source is not None else b"",
                                time.monotonic())
        if len(self._rows) > 2000:
            self._rows.popitem(last=False)

    def rows(self, uid: int, lang: str) -> list[Entry]:
        self._expire()
        return [e for e in reversed(self._rows.values()) if e.uid == uid and e.lang == lang]

    def exact(self, uid: int, chat: int, mid: int, lang: str, source: str) -> Entry | None:
        self._expire()
        e = self._rows.get((uid, chat, mid, lang))
        return e if e and e.source_hash == hashlib.sha256(source.encode()).digest() else None

    def forget(self, uid: int) -> None:
        for key in list(self._rows):
            if key[0] == uid:
                del self._rows[key]
