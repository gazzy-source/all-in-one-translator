"""Multi-account group simulation. Telegram and providers are mocked unless the live benchmark injects providers."""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramBadRequest

from tests.conftest import BOT_ID, GROUP_ID, OWNER, Call, FakeProvider, FakeSession, FakeTelegram, group_message
from tests.test_e2e_group import Harness, _cmd, make_settings
from translator_bot.app import App
from translator_bot.handlers import build_routers
from translator_bot.providers.base import TranslationJob, TranslationProvider, TranslationResult

TEXTS = {
    "en": "The meeting starts tomorrow at 10:00. Please bring the project notes with you.",
    "es": "La reunión empieza mañana a las 10:00. Por favor, trae las notas del proyecto.",
    "hi": "बैठक कल 10:00 बजे शुरू होगी। कृपया परियोजना के नोट्स अपने साथ लाएँ।",
    "my": "အစည်းအဝေးကို မနက်ဖြန် 10:00 တွင် စတင်မည်။ စီမံကိန်းမှတ်စုများကို ယူလာပါ။",
    "ar": "يبدأ الاجتماع غدًا الساعة 10:00. يرجى إحضار ملاحظات المشروع معك.",
    "ru": "Встреча начнётся завтра в 10:00. Пожалуйста, возьмите с собой заметки по проекту.",
    "ja": "会議は明日の10:00に始まります。プロジェクトのメモを持参してください。",
    "zh": "会议明天10:00开始。请带上项目笔记。",
}
READERS = dict(zip(TEXTS, [OWNER, 801, 802, 803, 804, 805, 806, 807], strict=True))
SECOND_ENGLISH, UNAPPROVED, PAUSED, MUTED, LEFT = 808, 900, 901, 902, 903
SECOND_GROUP = GROUP_ID - 1
ACTIVE = {uid: lang for lang, uid in READERS.items()} | {SECOND_ENGLISH: "en"}


class GroupTelegram(FakeTelegram):
    """Receiver-scoped bubbles with membership/ownership checks and controllable expiry."""

    def __init__(self) -> None:
        members = set(ACTIVE) | {UNAPPROVED, PAUSED, MUTED, BOT_ID}
        super().__init__(members={GROUP_ID: set(members), SECOND_GROUP: set(members)},
                         admins={GROUP_ID, SECOND_GROUP})
        self.visible: dict[tuple[int, int, int], str] = {}
        self.expired: set[tuple[int, int, int]] = set()

    def respond(self, method: Any) -> Any:
        name = type(method).__name__
        if (name == "GetChatMember" and method.chat_id == GROUP_ID and method.user_id == READERS["es"]
                and method.user_id in self.members[GROUP_ID]):
            return {"status": "creator", "user": {"id": method.user_id, "is_bot": False, "first_name": "Admin"},
                    "is_anonymous": False}
        params = method.model_dump(exclude_none=True)
        receiver = params.get("ephemeral_message_parameters", {}).get("receiver_user_id")
        if name == "EditEphemeralMessageText":
            receiver = params["receiver_user_id"]
        if receiver is not None:
            chat = params["chat_id"]
            if receiver not in self.members.get(chat, set()):
                self.calls.append(Call(name, params))
                raise TelegramBadRequest(method=method, message="USER_NOT_PARTICIPANT")
            if name == "EditEphemeralMessageText":
                key = (chat, receiver, params["ephemeral_message_id"])
                if key not in self.visible or key in self.expired:
                    self.calls.append(Call(name, params))
                    raise TelegramBadRequest(method=method, message="ephemeral message not found")
                self.visible[key] = params["text"]
        result = super().respond(method)
        if name == "SendMessage" and receiver is not None:
            self.visible[(params["chat_id"], receiver, result["ephemeral_message_id"])] = params["text"]
        return result


class MatrixProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__("gemini")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def translate(self, job: TranslationJob, *, quality: bool, timeout: float) -> TranslationResult:
        self.jobs.append((job, quality))
        self.started.set()
        await self.release.wait()
        source = next((lang for lang, text in TEXTS.items() if text == job.text), job.source_hint)
        return TranslationResult(True, TEXTS[job.target], source, 1.0, self.name, "matrix-fixture")


@asynccontextmanager
async def matrix_group(tmp_path: Path, detector: Any,
                       providers: dict[str, TranslationProvider] | None = None) -> AsyncIterator[Harness]:
    tg = GroupTelegram()
    provider = MatrixProvider()
    app = App(make_settings(tmp_path, EPHEMERAL_PER_CHAT_PER_MIN=10000, GLOBAL_SENDS_PER_S=10000),
              session=FakeSession(tg), providers=providers or {"gemini": provider}, detector=detector)
    await app.start()
    try:
        for chat in (GROUP_ID, SECOND_GROUP):
            await app.storage.upsert_chat(chat, title="Simulated Group", type="supergroup", enabled=True,
                                         bot_is_admin=True)
        for uid, lang in ACTIVE.items():
            await app.storage.set_approved(uid, True, by=OWNER)
            await app.storage.upsert_user(uid, lang=lang, onboarded=True, default_notice_sent=True)
        for uid in (PAUSED, MUTED, LEFT):
            await app.storage.set_approved(uid, True, by=OWNER)
            await app.storage.upsert_user(uid, lang="hi", onboarded=True)
        await app.storage.upsert_user(PAUSED, paused=True)
        await app.storage.upsert_user(UNAPPROVED, lang="hi", onboarded=True)
        for chat in (GROUP_ID, SECOND_GROUP):
            await app.storage.set_muted(MUTED, chat, True)
        dp = Dispatcher()
        for router in build_routers(app):
            dp.include_router(router)
        yield Harness(app, dp, tg, provider, provider)
        assert tg.public_group_output() == []
        assert app.guard.blocked == 0
    finally:
        await app.stop()


@pytest.fixture
async def matrix(tmp_path: Path, detector: Any) -> AsyncIterator[Harness]:
    async with matrix_group(tmp_path, detector) as harness:
        yield harness


@pytest.mark.parametrize("source", TEXTS)
async def test_eight_language_recipient_matrix(matrix: Harness, source: str) -> None:
    await matrix.send(group_message(TEXTS[source], sender=READERS[source]))
    await matrix.idle(timeout=20)
    for uid, target in ACTIVE.items():
        sends = matrix.tg.ephemeral_to(uid)
        assert len(sends) == int(target != source), (source, target, uid)
        if sends:
            assert sends[0].params["text"] == TEXTS[target]
    for uid in (UNAPPROVED, PAUSED, MUTED, LEFT):
        assert matrix.tg.ephemeral_to(uid) == [] and matrix.tg.dms_to(uid) == []
    # Two English readers share one provider request, but get separate receiver-only messages.
    assert Counter(job.target for job, _ in matrix.gemini.jobs) == Counter(lang for lang in TEXTS if lang != source)


async def test_group_admin_approval_never_leaks_to_second_group(matrix: Harness) -> None:
    await matrix.send(_cmd("/approve", READERS["es"], reply_to=group_message("hello", sender=UNAPPROVED)))
    matrix.tg.calls.clear()
    for chat in (GROUP_ID, SECOND_GROUP):
        await matrix.send(group_message(TEXTS["en"], sender=OWNER, chat_id=chat))
        await matrix.idle(timeout=20)
    sends = matrix.tg.ephemeral_to(UNAPPROVED)
    assert len(sends) == 1 and sends[0].params["chat_id"] == GROUP_ID
    assert sends[0].params["text"] == TEXTS["hi"]


async def test_all_access_changes_during_translation_are_respected(matrix: Harness) -> None:
    provider = matrix.gemini
    assert isinstance(provider, MatrixProvider)
    provider.release.clear()
    await matrix.send(group_message(TEXTS["en"], sender=OWNER))
    await asyncio.wait_for(provider.started.wait(), timeout=5)
    try:
        await matrix.app.storage.set_approved(READERS["es"], False, by=OWNER)
        await matrix.app.storage.upsert_user(READERS["hi"], paused=True)
        await matrix.app.storage.set_muted(READERS["my"], GROUP_ID, True)
        matrix.tg.members[GROUP_ID].discard(READERS["ar"])
        matrix.app.membership.invalidate(GROUP_ID, READERS["ar"])
        await matrix.app.storage.upsert_user(READERS["ru"], lang="en")
        await matrix.app.storage.upsert_user(READERS["ja"], also_reads="en")
    finally:
        provider.release.set()
    await matrix.idle(timeout=20)
    for uid in ACTIVE:
        assert len(matrix.tg.ephemeral_to(uid)) == int(uid == READERS["zh"])


async def test_topics_keep_consecutive_bubbles_separate(matrix: Harness) -> None:
    originals = []
    for topic in (10, 20, 10):
        message = group_message(TEXTS["es"], sender=READERS["es"],
                                extra={"is_topic_message": True, "message_thread_id": topic})
        originals.append(message["message_id"])
        await matrix.send(message)
        await matrix.idle(timeout=20)
    sends = matrix.tg.ephemeral_to(OWNER)
    assert [s.params["message_thread_id"] for s in sends] == [10, 20]
    [edit] = matrix.tg.ephemeral_edits_to(OWNER)
    assert edit.params["text"] == TEXTS["en"] + "\n" + TEXTS["en"]
    first = await matrix.app.storage.deliveries_for(GROUP_ID, originals[0])
    owner = next(row for row in first if row["recipient_id"] == OWNER)
    assert edit.params["ephemeral_message_id"] == owner["ephemeral_id"]


async def test_expired_bubbles_recover_for_every_language(matrix: Harness) -> None:
    assert isinstance(matrix.tg, GroupTelegram)
    await matrix.send(group_message(TEXTS["es"], sender=READERS["es"]))
    await matrix.idle(timeout=20)
    matrix.tg.expired.update(matrix.tg.visible)
    await matrix.send(group_message(TEXTS["es"], sender=READERS["es"]))
    await matrix.idle(timeout=20)
    for uid, lang in ACTIVE.items():
        assert len(matrix.tg.ephemeral_to(uid)) == (0 if lang == "es" else 2)


async def test_concurrent_multilingual_messages_keep_recipients_isolated(matrix: Harness) -> None:
    messages = [group_message(TEXTS[lang], sender=READERS[lang]) for _ in range(3) for lang in TEXTS]
    for message in messages:
        await matrix.send(message)
    await matrix.idle(timeout=30)
    for message in messages:
        source = next(lang for lang, text in TEXTS.items() if text == message["text"])
        rows = await matrix.app.storage.deliveries_for(GROUP_ID, message["message_id"])
        assert {row["recipient_id"] for row in rows} == {uid for uid, lang in ACTIVE.items() if lang != source}
        assert all(row["status"] == "sent" and row["lang"] == ACTIVE[row["recipient_id"]] for row in rows)
