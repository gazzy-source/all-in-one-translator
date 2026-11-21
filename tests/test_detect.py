"""Language gate tests. The full labelled corpus also runs as a regression gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from translator_bot.detect import LanguageDetector, Verdict

CORPUS = json.loads((Path(__file__).parent.parent / "benchmarks" / "detection_corpus.json").read_text("utf-8"))


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: f"{c['target']}:{c['text'][:30]}")
def test_corpus_case(detector: LanguageDetector, case: dict[str, str]) -> None:
    d = detector.detect(case["text"], case["target"])
    if case["expect"] == "skip":
        assert d.verdict is not Verdict.OTHER, f"false translation: {d}"
    elif case["expect"] == "translate":
        assert d.verdict is not Verdict.TARGET, f"missed translation: {d}"
        if case.get("lang") and d.verdict is Verdict.OTHER and d.lang:
            # ms/id are mutually intelligible and often indistinguishable on short text
            assert d.lang == case["lang"] or {d.lang, case["lang"]} == {"ms", "id"}


def test_unsure_rate_bounded(detector: LanguageDetector) -> None:
    unsure = sum(detector.detect(c["text"], c["target"]).verdict is Verdict.UNSURE for c in CORPUS)
    assert unsure / len(CORPUS) <= 0.12  # deferred-to-provider budget


@pytest.mark.parametrize(("text", "lang"), [
    ("kal milte hai bhai", "hi"), ("mujhe nahi pata yaar", "hi"), ("bhai tu kab aa raha hai?", "hi")])
def test_romanized_hindi(detector: LanguageDetector, text: str, lang: str) -> None:
    d = detector.detect(text, "en")
    assert d.verdict is Verdict.OTHER and d.lang == lang and d.romanized


def test_romanized_hindi_is_target_for_hindi_reader(detector: LanguageDetector) -> None:
    assert detector.detect("kal milte hai bhai", "hi").verdict is Verdict.TARGET


def test_mixed_language(detector: LanguageDetector) -> None:
    d = detector.detect("bro, esto está insane", "en")
    assert d.verdict is Verdict.OTHER and d.lang == "es"
    d = detector.detect("मैं ठीक हूँ, thanks for asking", "en")
    assert d.mixed and d.verdict is not Verdict.TARGET


def test_neutral_and_laughter(detector: LanguageDetector) -> None:
    for t in ("ok", "lol", "jajaja", "😂", "gg wp"):
        d = detector.detect(t, "es")
        assert d.verdict is Verdict.TARGET and d.neutral


def test_weak_evidence_is_not_confident(detector: LanguageDetector) -> None:
    for t in ("Naruto", "Mohammed", "si"):
        assert detector.detect(t, "en").verdict is Verdict.UNSURE


def test_same_script_neighbours(detector: LanguageDetector) -> None:
    assert detector.detect("Привет, как дела?", "en").lang == "ru"
    assert detector.detect("Привіт, як справи?", "en").lang == "uk"
    assert detector.detect("Привіт, як справи?", "ru").verdict is not Verdict.TARGET
