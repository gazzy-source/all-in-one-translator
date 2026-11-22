"""Bounded, in-memory conversation context per (chat, topic). Never persisted; expires after a TTL.

Speakers are pseudonymized ("A", "B", ...) before anything is sent to a provider.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from translator_bot.providers.base import ContextLine
from translator_bot.textutil import truncate


@dataclass(slots=True)
class _Entry:
    ts: float
    sender_id: int
    text: str
    seq: int = 0


class ContextStore:
    def __init__(self, max_messages: int = 6, ttl_s: float = 900, max_chats: int = 5000,
                 max_chars: int = 280) -> None:
        self.max_messages, self.ttl_s, self.max_chats, self.max_chars = max_messages, ttl_s, max_chats, max_chars
        self._buf: OrderedDict[tuple[int, int | None], deque[_Entry]] = OrderedDict()
        self._seq = 0

    @property
    def mark(self) -> int:
        """Position in the stream: get(before=mark) returns only lines added before this moment."""
        return self._seq + 1

    def add(self, chat_id: int, thread_id: int | None, sender_id: int, text: str) -> None:
        if self.max_messages <= 0 or not text.strip():
            return
        self._seq += 1
        key = (chat_id, thread_id)
        dq = self._buf.get(key)
        if dq is None:
            dq = self._buf[key] = deque(maxlen=self.max_messages)
            while len(self._buf) > self.max_chats:
                self._buf.popitem(last=False)
        self._buf.move_to_end(key)
        dq.append(_Entry(time.monotonic(), sender_id, truncate(text, self.max_chars), self._seq))

    def get(self, chat_id: int, thread_id: int | None, current_sender: int,
            limit: int | None = None, before: int | None = None) -> tuple[ContextLine, ...]:
        """Recent lines, oldest first. `before` (a `mark` taken when the message arrived) excludes the message
        itself, the rest of its burst and anything posted while it waited in the queue."""
        dq = self._buf.get((chat_id, thread_id))
        if not dq:
            return ()
        now = time.monotonic()
        entries = [e for e in dq if now - e.ts <= self.ttl_s and (before is None or e.seq < before)]
        if limit is not None:
            entries = entries[-limit:]
        labels: dict[int, str] = {current_sender: "A"}
        out = []
        for e in entries:
            if e.sender_id not in labels:
                labels[e.sender_id] = chr(ord("A") + min(len(labels), 25))
            out.append(ContextLine(labels[e.sender_id], e.text))
        return tuple(out)

    def speaker_label(self, chat_id: int, thread_id: int | None, current_sender: int, other: int) -> str:
        if other == current_sender:
            return "A"
        dq = self._buf.get((chat_id, thread_id)) or ()
        labels: dict[int, str] = {current_sender: "A"}
        for e in dq:
            if e.sender_id not in labels:
                labels[e.sender_id] = chr(ord("A") + min(len(labels), 25))
        return labels.get(other, "B" if len(labels) == 1 else chr(ord("A") + min(len(labels), 25)))

    def forget_chat(self, chat_id: int) -> None:
        for key in [k for k in self._buf if k[0] == chat_id]:
            del self._buf[key]

    def clear(self) -> None:
        self._buf.clear()
