"""Multi-signal language detection, framed as a three-way gate relative to the target language.

Signals (all local, no network):
  1. Unicode script analysis (decisive for Hangul, Kana, Greek, Thai, Hebrew, ...).
  2. Word-frequency evidence from `wordfreq` — far more reliable than character n-grams for short,
     informal chat ("no manches", "ni modo", "ça va?").
  3. `lingua` (character n-grams) for longer text and for languages sharing a script (ru/uk/bg, ar/fa/ur).
  4. Lexicons: romanized Hindi/Urdu (Hinglish), Arabizi, English chat slang, and universal chat tokens
     ("ok", "lol", "haha") that carry no language at all.

The gate answers "does this need translating into <target>?" with TARGET / OTHER / UNSURE.
UNSURE text is not guessed: the LLM provider decides it in the same call that translates.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from enum import Enum
from importlib import resources
from typing import Any

import regex as re
from wordfreq import zipf_frequency

from translator_bot.languages import LANGUAGES
from translator_bot.textutil import LAUGH_RE, ContentKind, profile_text, script_counts

log = logging.getLogger(__name__)


class Verdict(Enum):
    TARGET = "target"  # already in the target language (or language-neutral) -> do not translate
    OTHER = "other"  # confidently a different language -> translate
    UNSURE = "unsure"  # let the provider decide (structured `needs_translation`)


@dataclass(slots=True)
class Detection:
    lang: str | None
    confidence: float
    verdict: Verdict
    romanized: bool = False  # e.g. Hindi written in Latin script
    mixed: bool = False  # meaningful content in 2+ languages
    informal: bool = False  # slang / laughter / chat shorthand present
    neutral: bool = False  # only universal tokens ("ok", "lol")
    signals: dict[str, object] = field(default_factory=dict)


# Universal chat tokens: understood everywhere, carry no language signal.
NEUTRAL_TOKENS = frozenset(
    ["ok", "okay", "okey", "oki", "okk", "k", "kk", "lol", "lool", "lmao", "lmfao", "rofl", "omg", "xd", "xdd", "haha", "hahaha", "hehe", "hihi", "gg", "wp", "ggwp", "wow", "ooh", "oh", "ah", "uh", "um", "hmm", "hm", "mm", "mmm", "yay", "yey", "yo", "bye", "hi", "hello", "hey", "ciao", "salut", "hola", "bravo", "bro", "bruh", "sis", "pls", "plz", "thx", "ty", "np", "ez", "noob", "pro", "vip", "wtf", "brb", "afk"]
)
# English chat slang/abbreviations wordfreq under-weights.
EN_SLANG = frozenset(
    ["idk", "idc", "imo", "imho", "tbh", "ngl", "smh", "fr", "frfr", "rn", "lmk", "wyd", "hbu", "hmu", "btw", "fyi", "irl", "jk", "nvm", "ikr", "ily", "tho", "gonna", "wanna", "gotta", "kinda", "sorta", "ya", "yeah", "yep", "yup", "nope", "nah", "u", "ur", "r", "cuz", "coz", "bc", "dunno", "lemme", "gimme", "ain't", "y'all", "sus", "lit", "bet", "cap", "lowkey", "highkey", "deadass", "bussin", "slay", "vibe", "vibes", "goat", "simp"]
)
ARABIZI_TOKENS = frozenset(
    ["habibi", "habibti", "yalla", "wallah", "walla", "inshallah", "inshalla", "mashallah", "ana", "enta", "inta", "enti", "inti", "shu", "shou", "keef", "kifak", "kifik", "kaifa", "ahlan", "marhaba", "shukran", "yani", "ya3ni", "3arabi", "7abibi", "7ayati", "mafi", "fi", "khalas", "akh", "ukhti"]
)
ARABIZI_DIGIT_RE = re.compile(r"(?i)^(?=.*[a-z])(?=.*[2-9])[a-z2-9']{3,}$")

# Latin-script languages scored with wordfreq (must be in wordfreq's supported set).
WORDFREQ_LATIN = (
    "en", "es", "fr", "pt", "it", "de", "nl", "pl", "ro", "cs", "sk", "sl", "hu", "fi", "sv", "da", "nb",
    "tr", "id", "ms", "fil", "ca", "vi", "lt", "lv", "is",
)
WORDFREQ_NONLATIN = frozenset({"ru", "uk", "bg", "mk", "ar", "fa", "ur", "he", "el"})
_WF_TO_ISO = {"fil": "tl"}
_ISO_TO_WF = {"tl": "fil"}

# Script -> candidate languages that use it.
_SCRIPT_LANGS: dict[str, tuple[str, ...]] = {}
for _l in LANGUAGES.values():
    _SCRIPT_LANGS.setdefault(_l.script, tuple())
    _SCRIPT_LANGS[_l.script] = (*_SCRIPT_LANGS[_l.script], _l.code)


def _load_lexicon(name: str) -> dict[str, float]:
    out: dict[str, float] = {}
    text = resources.files("translator_bot.data").joinpath(name).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        out[parts[0].lower()] = float(parts[1]) if len(parts) > 1 else 1.0
    return out


_EXTRA_LAUGH_RE = re.compile(r"^(?:ه{3,}|(?:हा){2,}|(?:ха){2,}х?|5{3,}|h{3,}|xd+)$", re.IGNORECASE)
# Korean crying/laughing jamo and similar 2-char chat tokens (DeepL leaves "ㅠㅠ" as-is in Arabic/Spanish)
_SHORT_CHAT_TOKENS = frozenset({"ㅠㅠ", "ㅜㅜ", "ㅋㅋ", "ㅎㅎ", "ㅠㅠㅠ", "ㅜㅜㅜ"})
_PUNCT_STRIP = "\"'.,!?¿¡()[]{}:;~…-—–*"


def _has_laughter(text: str) -> bool:
    """Laughter tokens of >= 3 characters in any script (short ones like "hi"/"ja"/"k" are real words)."""
    for tok in text.split():
        t = tok.strip(_PUNCT_STRIP).lower()
        if t in _SHORT_CHAT_TOKENS or (len(t) >= 3 and (LAUGH_RE.match(t) or _EXTRA_LAUGH_RE.match(t))):
            return True
    return False


# Common characters that exist in only one of the two scripts (simplified, traditional) - pairs of the same word.
_SIMPLIFIED_ONLY = frozenset("们这个来说时会对国为么过还没后发见让给东车门问间听话关开学习买卖钱电视气爱样经长现从种")
_TRADITIONAL_ONLY = frozenset("們這個來說時會對國為麼過還沒後發見讓給東車門問間聽話關開學習買賣錢電視氣愛樣經長現從種")


def _han_variant(text: str) -> str | None:
    """'zh' (Simplified), 'zh-Hant' (Traditional), or None when the text uses only shared characters."""
    simp = sum(ch in _SIMPLIFIED_ONLY for ch in text)
    trad = sum(ch in _TRADITIONAL_ONLY for ch in text)
    if simp == trad:
        return None
    return "zh" if simp > trad else "zh-Hant"


class LanguageDetector:
    """Thread-safe after construction. Construct once at startup (loads models/lexicons)."""

    def __init__(self, preload: bool = True, wordlist: str = "best") -> None:
        # "small" keeps only common words per language (~0.1 GB instead of ~1 GB of frequency tables)
        self._wordlist = wordlist
        self._hinglish = _load_lexicon("hinglish.txt")
        self._burglish = _load_lexicon("burglish.txt")
        self._lingua: Any = None
        self._lingua_lock = threading.Lock()
        self._lingua_map: dict[object, str] = {}
        if preload:
            self.warmup()

    # -- lazy heavy resources -------------------------------------------------------------------
    def _get_lingua(self) -> Any:
        if self._lingua is None:
            with self._lingua_lock:
                if self._lingua is None:
                    from lingua import IsoCode639_1, Language, LanguageDetectorBuilder

                    langs = []
                    for code in LANGUAGES:
                        base = code.split("-")[0]
                        try:
                            iso = IsoCode639_1.from_str(base)
                            lang = Language.from_iso_code_639_1(iso)
                        except (ValueError, AttributeError):
                            continue
                        if lang not in langs:
                            langs.append(lang)
                            self._lingua_map[lang] = "zh" if base == "zh" else base
                    self._lingua = (
                        LanguageDetectorBuilder.from_languages(*langs).with_preloaded_language_models().build()
                    )
        return self._lingua

    def warmup(self) -> None:
        for lang in WORDFREQ_LATIN:
            self._zipf("the", lang)  # loads frequency tables (first call is slow: ~2s total)
        self._get_lingua()

    def _zipf(self, word: str, lang: str) -> float:
        return float(zipf_frequency(word, lang, wordlist=self._wordlist))

    # -- public ----------------------------------------------------------------------------------
    def detect(self, text: str, target: str) -> Detection:
        prof = profile_text(text)
        if prof.kind in (ContentKind.EMPTY, ContentKind.EMOJI_ONLY, ContentKind.URL_ONLY,
                         ContentKind.MENTION_ONLY, ContentKind.NUMERIC, ContentKind.COMMAND):
            return Detection(None, 1.0, Verdict.TARGET, neutral=True, signals={"kind": prof.kind.value})
        if prof.kind is ContentKind.LAUGHTER:
            return Detection(None, 1.0, Verdict.TARGET, neutral=True, informal=True, signals={"kind": "laughter"})

        words = [w.lower().strip("'") for w in prof.words if w.strip("'")]
        scripts = script_counts(prof.core)
        total = sum(scripts.values()) or 1
        latin = scripts.get("Latin", 0)
        non_latin = {k: v for k, v in scripts.items() if k != "Latin"}
        dominant = max(scripts, key=lambda k: scripts[k]) if scripts else "Latin"

        if non_latin and sum(non_latin.values()) / total >= 0.5:
            det = self._detect_non_latin(prof.core, dominant, target)
            # Latin share >= 20% alongside: mixed (e.g. Hindi + English code-switching)
            if latin / total >= 0.2:
                det.mixed = True
            # chat laughter ("555", "ㅋㅋㅋ", "хахаха", "ههههه", "草") marks informal chat -> the LLM route
            # (DeepL leaves "555" untranslated and renders slang as textbook prose)
            if _has_laughter(text):
                det.informal = True
            return det
        det = self._detect_latin(prof.core, words, target)
        if det.lang == "vi" and prof.core.isascii():
            det.informal = True  # Vietnamese typed without diacritics: DeepL misreads it (live: "hotpot" lost)
            # ...also as a fallback (holdout 2026-11-22: Gemini timed out, DeepL said "I haven't been to a
            # restaurant in a while"): treat it like other romanized text - Gemini only, no DeepL
            det.romanized = True
        if non_latin and sum(non_latin.values()) / total >= 0.15:
            det.mixed = True
            if det.verdict is Verdict.TARGET:
                det.verdict = Verdict.UNSURE
        return det

    # -- non-Latin -------------------------------------------------------------------------------
    def _detect_non_latin(self, core: str, script: str, target: str) -> Detection:
        candidates = _SCRIPT_LANGS.get(script, ())
        target_base = target.split("-")[0]
        lang: str | None
        if len(candidates) == 1:
            lang = candidates[0]
            conf = 0.97
        else:
            lang, conf = self._lingua_best(core, candidates)
            wf_lang, wf_conf = self._wordfreq_best(core, candidates)
            if wf_lang is not None and (lang is None or wf_lang == lang):
                lang, conf = wf_lang, max(conf, wf_conf)
            elif wf_lang is not None and wf_conf > conf:
                lang, conf = wf_lang, (wf_conf + (1 - conf)) / 2 * 0.8
            if lang is None:
                lang, conf = (candidates[0] if candidates else None), 0.5
            elif lang not in WORDFREQ_NONLATIN:
                # lingua picked a language word lists can't check (be, kk, ky, mn, tg...): if most words are listed
                # (and half are common) in a word-list language, that's the language ("Да ладно, не вешай нос"
                # was "be", 0.81). The best-covered language wins, not the zipf vote (small lists: "mk").
                words = [w.lower() for w in re.findall(r"[\p{L}\p{M}]+", core)]
                best_cov: tuple[float, float, str] | None = None
                for cand in (c for c in candidates if c in WORDFREQ_NONLATIN):
                    zs = [self._zipf(w, cand) for w in words]
                    known = sum(1 for z in zs if z > 0) / max(1, len(zs))
                    common = sum(1 for z in zs if z >= 3.0) / max(1, len(zs))
                    if words and known >= 0.6 and common >= 0.5 and (best_cov is None or (known, common) >
                                                                    best_cov[:2]):
                        best_cov = (known, common, cand)
                if best_cov is not None:
                    lang, conf = best_cov[2], max(conf, wf_conf if wf_lang == best_cov[2] else 0.6)
        if lang == "zh" and target_base == "zh":
            # Simplified vs Traditional readers are different targets: decide from distinguishing characters
            variant = _han_variant(core)
            want = "zh-Hant" if target.lower() in ("zh-hant", "zh-tw", "zh-hk", "zh-mo") else "zh"
            if variant is not None and variant != want:
                return Detection(variant, 0.9, Verdict.OTHER, signals={"script": script, "han_variant": variant})
            return Detection(want, conf, Verdict.TARGET, signals={"script": script, "han_variant": variant})
        if lang and lang.split("-")[0] == target_base:
            verdict = Verdict.TARGET if conf >= 0.5 else Verdict.UNSURE
        elif target_base in {c.split("-")[0] for c in candidates} and conf < 0.75:
            verdict = Verdict.UNSURE  # same script as target, weak evidence (ru vs uk, hi vs mr)
        else:
            verdict = Verdict.OTHER
        return Detection(lang, conf, verdict, signals={"script": script})

    def _wordfreq_best(self, text: str, candidates: tuple[str, ...]) -> tuple[str | None, float]:
        """Word-frequency vote among same-script candidates wordfreq supports (ru/uk/bg/mk, ar/fa/ur...)."""
        langs = [c for c in candidates if c in WORDFREQ_NONLATIN]
        if len(langs) < 2:
            return None, 0.0
        words = [w.lower() for w in re.findall(r"[\p{L}\p{M}]+", text)]
        if not words:
            return None, 0.0
        scores = {lang: sum(self._zipf(w, lang) for w in words) / len(words) for lang in langs}
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if ranked[0][1] < 1.5:
            return None, 0.0
        margin = ranked[0][1] - ranked[1][1]
        conf = 1 / (1 + math.exp(-2.5 * (margin - 0.4)))
        return ranked[0][0], conf

    def _lingua_best(self, text: str, candidates: tuple[str, ...]) -> tuple[str | None, float]:
        detector = self._get_lingua()
        allowed = {c.split("-")[0] for c in candidates}
        best: tuple[str | None, float] = (None, 0.0)
        total = 0.0
        for cv in detector.compute_language_confidence_values(text):
            code = self._lingua_map.get(cv.language)
            if code in allowed:
                total += cv.value
                if cv.value > best[1]:
                    best = (code, cv.value)
        if best[0] is None or total <= 0:
            return None, 0.0
        return best[0], best[1] / total  # renormalized over the plausible candidates

    # -- Latin -----------------------------------------------------------------------------------
    def _detect_latin(self, core: str, words: list[str], target: str) -> Detection:
        target_base = target.split("-")[0]
        content = [w for w in words if w not in NEUTRAL_TOKENS]
        if not content:
            return Detection(None, 1.0, Verdict.TARGET, neutral=True, informal=True, signals={"neutral": True})

        informal = any(w in EN_SLANG or w in NEUTRAL_TOKENS or LAUGH_RE.match(w) for w in words)
        n = len(content)

        # Lexicon evidence
        hing = sum(self._hinglish.get(w, 0.0) for w in content)
        hing_hits = sum(1 for w in content if self._hinglish.get(w, 0.0) >= 0.6)
        arabizi = sum(1 for w in content if w in ARABIZI_TOKENS or ARABIZI_DIGIT_RE.match(w))
        slang = sum(1 for w in content if w in EN_SLANG)

        # Word-frequency evidence per language
        scores: dict[str, float] = {}
        coverage: dict[str, float] = {}
        per_token_best: list[str | None] = []
        token_tables = [{lang: self._zipf(w, lang) for lang in WORDFREQ_LATIN} for w in content]
        token_max = [max(t.values()) for t in token_tables]
        for lang in WORDFREQ_LATIN:
            zs = [t[lang] for t in token_tables]
            scores[lang] = sum(zs) / n
            # A token "covers" a language only if it is common there AND close to its best language:
            # internet-scraped tables put many foreign words into English at zipf ~3.
            coverage[lang] = sum(1 for z, mx in zip(zs, token_max, strict=True) if z >= 2.5 and z >= mx - 1.0) / n
        for tbl in token_tables:
            top = max(tbl, key=lambda k: tbl[k])
            per_token_best.append(top if tbl[top] >= 2.0 else None)
        # English slang counts as English evidence
        if slang:
            scores["en"] += 2.5 * slang / n
            coverage["en"] = min(1.0, coverage["en"] + slang / n)

        # Lingua for longer text, restricted to Latin candidates
        lingua_lang: str | None = None
        lingua_conf = 0.0
        if len(content) >= 4 or len(core) >= 30:
            latin_codes = tuple(_WF_TO_ISO.get(c, c) for c in WORDFREQ_LATIN)
            lingua_lang, lingua_conf = self._lingua_best(core, latin_codes)
            if lingua_lang:
                wf = _ISO_TO_WF.get(lingua_lang, lingua_lang)
                if wf in scores:
                    scores[wf] += 1.5 * lingua_conf

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best_wf, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        best = _WF_TO_ISO.get(best_wf, best_wf)
        margin = best_score - second_score
        conf = 1 / (1 + math.exp(-2.2 * (margin - 0.45)))  # margin ~0.45 zipf -> 0.5 confidence
        conf *= min(1.0, 0.55 + 0.15 * n)  # fewer words -> less certainty
        signals: dict[str, object] = {
            "wf_best": best_wf, "wf_margin": round(margin, 2), "lingua": lingua_lang,
            "hinglish": round(hing, 2), "arabizi": arabizi, "n": n,
        }

        # Romanized Burmese ("Burglish"; live 2026-11-22: "mingalarpar, nay kaung lar?" was read as Tagalog and
        # DeepL invented "why are you staring at my feet"). Stricter than Hinglish: short tokens overlap.
        burm_w = [self._burglish.get(w, 0.0) for w in content]
        burm, burm_hits = sum(burm_w), sum(1 for x in burm_w if x >= 0.6)
        burglish = ((burm_hits >= 2 and burm / n >= 0.34) or (burm_hits >= 3 and burm / n >= 0.25)
                    or (max(burm_w, default=0.0) >= 1.0 and burm / n >= 0.5))
        if burglish and burm > hing and not (best == "en" and coverage["en"] >= 0.9):
            verdict = Verdict.TARGET if target_base == "my" else Verdict.OTHER
            mixed = coverage["en"] >= 0.25 and best == "en"
            return Detection("my", min(0.9, 0.5 + 0.1 * burm_hits), verdict, romanized=True, mixed=mixed,
                             informal=True, signals=signals | {"rule": "burglish", "burglish": round(burm, 2)})

        # Romanized Hindi: needs at least one strong token and lexicon mass over ~1/3 of tokens.
        hinglish =(hing_hits >= 1 and hing / n >= 0.34) or (hing_hits >= 2 and hing / n >= 0.25)
        if hinglish and not (best == "en" and coverage["en"] >= 0.9 and hing_hits < 2):
            romanized_conf = min(0.95, 0.5 + 0.15 * hing_hits)
            verdict = Verdict.TARGET if target_base in ("hi", "ur") else Verdict.OTHER
            mixed = coverage["en"] >= 0.25 and best == "en"
            return Detection("hi", romanized_conf, verdict, romanized=True, mixed=mixed, informal=True,
                             signals=signals | {"rule": "hinglish"})
        if arabizi >= 1 and arabizi / n >= 0.34:
            verdict = Verdict.TARGET if target_base == "ar" else Verdict.OTHER
            return Detection("ar", 0.7, verdict, romanized=True, informal=True, signals=signals | {"rule": "arabizi"})

        # Mixed-language: target words plus a meaningful share of confidently-foreign words.
        target_wf = _ISO_TO_WF.get(target_base, target_base)
        foreign_tokens = [
            w for w, tbl, top in zip(content, token_tables, per_token_best, strict=True)
            if top and top != target_wf and tbl.get(target_wf, 0.0) < 1.5 and w not in EN_SLANG
        ]
        foreign_share = len(foreign_tokens) / n
        target_cov = coverage.get(target_wf, 0.0)

        if target_wf in scores and best_wf == target_wf:
            if foreign_share >= 0.3 and len(foreign_tokens) >= 1:
                return Detection(best, conf, Verdict.OTHER, mixed=True, informal=informal,
                                 signals=signals | {"foreign": foreign_tokens[:5]})
            verdict = Verdict.TARGET if (conf >= 0.45 or target_cov >= 0.85) else Verdict.UNSURE
            return Detection(best, conf, verdict, informal=informal, signals=signals)

        mixed = target_cov >= 0.3 and foreign_share >= 0.3
        if best_score < 2.0 and not lingua_lang:
            # Unknown vocabulary (names, rare dialect, typos, unsupported language) -> ask the provider.
            return Detection(None, 0.2, Verdict.UNSURE, mixed=mixed, informal=informal, signals=signals)
        if conf >= 0.55 and (target_cov < 0.6 or foreign_share >= 0.3):
            return Detection(best, conf, Verdict.OTHER, mixed=mixed, informal=informal, signals=signals)
        if target_cov < 0.25 and best_score >= 2.5 and n >= 2:
            # Clearly not the target even if the exact source is uncertain (ms vs id, es vs pt):
            # translate, and let the provider pin down the source language.
            return Detection(best, conf, Verdict.OTHER, mixed=mixed, informal=informal,
                             signals=signals | {"rule": "not-target"})
        return Detection(best, conf, Verdict.UNSURE, mixed=mixed, informal=informal, signals=signals)
