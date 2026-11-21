from __future__ import annotations

import asyncio

from tests.conftest import FakeProvider
from translator_bot.detect import Detection, Verdict
from translator_bot.metrics import Metrics
from translator_bot.orchestrator import Orchestrator, validate
from translator_bot.providers.base import ContextLine, ErrorKind, TranslationJob, TranslationResult


def det(verdict: Verdict = Verdict.OTHER, lang: str = "es", **kw: bool) -> Detection:
    return Detection(lang, 0.9, verdict, **kw)


def orch(**providers: FakeProvider) -> Orchestrator:
    return Orchestrator(dict(providers), Metrics())


async def test_clean_text_routes_to_deepl() -> None:
    g, d = FakeProvider("gemini"), FakeProvider("deepl", supported={"es", "en"})
    o = orch(gemini=g, deepl=d)
    res = await o.translate(TranslationJob("Vamos a la playa mañana por la tarde", "en", "es"), det())
    assert res is not None and res.provider == "deepl" and not g.jobs


async def test_slang_romanized_mixed_route_to_gemini() -> None:
    g, d = FakeProvider("gemini"), FakeProvider("deepl", supported={"es", "en", "hi"})
    o = orch(gemini=g, deepl=d)
    await o.translate(TranslationJob("no manches wey", "en", "es"), det(informal=True))
    await o.translate(TranslationJob("kal milte hai bhai sab log", "en", "hi"), det(lang="hi", romanized=True))
    assert len(g.jobs) == 2 and not d.jobs


async def test_mixed_romanized_and_unsure_with_context_use_quality() -> None:
    g = FakeProvider("gemini")
    o = orch(gemini=g)
    await o.translate(TranslationJob("bhai this is so bakwas yaar", "en", "hi"),
                      det(lang="hi", romanized=True, mixed=True))
    await o.translate(TranslationJob("ni modo", "en", reply_to=ContextLine("B", "Are you coming?")),
                      det(Verdict.UNSURE))
    assert [q for _, q in g.jobs] == [True, True]


async def test_inline_never_uses_quality_model() -> None:
    g = FakeProvider("gemini")
    o = orch(gemini=g)
    await o.translate(TranslationJob("ni modo", "en", reply_to=ContextLine("B", "x")), det(Verdict.UNSURE),
                      purpose="inline")
    assert g.jobs[0][1] is False


async def test_fallback_one_hop_on_failure() -> None:
    g, d = FakeProvider("gemini"), FakeProvider("deepl", supported={"es", "en"})
    d.fail = [ErrorKind.QUOTA]
    o = orch(gemini=g, deepl=d)
    res = await o.translate(TranslationJob("Vamos a la playa mañana por la tarde", "en", "es"), det())
    assert res is not None and res.provider == "gemini"
    assert o.metrics.counters["fallback"] == 1


async def test_transient_retried_once_then_fallback() -> None:
    g, d = FakeProvider("gemini"), FakeProvider("deepl", supported={"es", "en"})
    g.fail = [ErrorKind.TRANSIENT, ErrorKind.TRANSIENT]
    o = orch(gemini=g, deepl=d)
    res = await o.translate(TranslationJob("no manches wey qué onda", "en", "es"), det(informal=True))
    assert res is not None and res.provider == "deepl" and len(g.jobs) == 2


async def test_all_providers_down_is_silent() -> None:
    g = FakeProvider("gemini")
    g.fail = [ErrorKind.AUTH]
    o = orch(gemini=g)
    assert await o.translate(TranslationJob("hola amigo", "en"), det()) is None
    assert o.breakers["gemini"].state == "open"  # auth errors open the breaker for a long time


async def test_romanized_never_sent_to_deepl_even_as_fallback() -> None:
    g, d = FakeProvider("gemini"), FakeProvider("deepl", supported={"hi", "en"})
    g.fail = [ErrorKind.QUOTA]
    o = orch(gemini=g, deepl=d)
    assert await o.translate(TranslationJob("kal milte hai bhai", "en", "hi"), det(lang="hi", romanized=True)) is None
    assert not d.jobs


async def test_low_confidence_is_silent() -> None:
    g = FakeProvider("gemini")
    g.confidence = 0.2
    o = orch(gemini=g)
    assert await o.translate(TranslationJob("asdkj qwe", "en"), det(Verdict.UNSURE)) is None


async def test_validation_escalates_to_quality_once() -> None:
    g = FakeProvider("gemini")
    g.overrides["meet at 7:30 at https://x.io"] = "nos vemos"  # drops number + URL
    o = orch(gemini=g)
    res = await o.translate(TranslationJob("meet at 7:30 at https://x.io", "es", "en"), det(lang="en", informal=True))
    assert [q for _, q in g.jobs] == [False, True]
    assert res is not None  # best effort: missing-entity result still better than silence


async def test_identical_output_treated_as_no_translation() -> None:
    g = FakeProvider("gemini")
    g.overrides["Naruto"] = "Naruto"
    o = orch(gemini=g)
    assert await o.translate(TranslationJob("Naruto", "en"), det(Verdict.UNSURE)) is None


async def test_cache_and_context_scoping() -> None:
    g = FakeProvider("gemini")
    o = orch(gemini=g)
    j = TranslationJob("qué onda", "en")
    await o.translate(j, det(informal=True), scope="1")
    await o.translate(TranslationJob("qué onda", "en"), det(informal=True), scope="2")
    assert len(g.jobs) == 1  # context-free: content-addressed, shared
    ctx = (ContextLine("B", "hi"),)
    await o.translate(TranslationJob("qué onda", "en", context=ctx), det(informal=True), scope="1")
    await o.translate(TranslationJob("qué onda", "en", context=ctx), det(informal=True), scope="2")
    assert len(g.jobs) == 3  # context-dependent: never shared across chats


async def test_concurrent_identical_requests_coalesced() -> None:
    g = FakeProvider("gemini", latency=0.05)
    o = orch(gemini=g)
    rs = await asyncio.gather(*(o.translate(TranslationJob("qué onda", "en"), det(informal=True)) for _ in range(5)))
    assert len(g.jobs) == 1 and all(r is not None for r in rs)


def test_validate_flags() -> None:
    j = TranslationJob("Hola, nos vemos a las 5", "en")

    def r(t: str) -> TranslationResult:
        return TranslationResult(True, t, "es", 0.9, "x", "m")

    assert validate(j, r("Hi, see you at 5")) == []
    assert "missing:1" in validate(j, r("Hi, see you later"))
    assert "preamble" in validate(j, r("Translation: Hi, see you at 5"))
    assert "too_long" in validate(j, r("Hi, see you at 5. " * 20))
    assert "empty" in validate(j, r("  "))


async def test_wrong_script_escalates_and_is_not_delivered() -> None:
    g = FakeProvider("gemini")
    g.overrides["boom bro"] = "בום אחי"  # Hebrew letters for a Hindi reader
    o = orch(gemini=g)
    res = await o.translate(TranslationJob("boom bro", "hi", "en"), det(lang="en", informal=True))
    assert res is None  # never delivered in the wrong script
    assert [q for _, q in g.jobs] == [False, True]  # escalated once


async def test_garbled_output_escalates_then_falls_back_to_deepl() -> None:
    g, d = FakeProvider("gemini"), FakeProvider("deepl", supported={"fr", "ru"})
    g.overrides["Grave, c'était ouf mdr, on remet ça samedi ?"] = "Жестче азец гага Чтев деет завтра?"
    o = orch(gemini=g, deepl=d)
    res = await o.translate(TranslationJob("Grave, c'était ouf mdr, on remet ça samedi ?", "ru", "fr"),
                            det(lang="fr", informal=True))
    assert [q for _, q in g.jobs] == [False, True]  # escalated once, still garbled
    assert res is not None and res.provider == "deepl"  # then the other provider family
