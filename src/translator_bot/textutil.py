"""Text classification, entity extraction, and Telegram-HTML sanitizing.

Everything here is pure and fast; it runs on every message before any provider call.
"""

from __future__ import annotations

import html
import unicodedata
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser

import regex as re  # third-party `regex` (dependency of wordfreq): supports \p{...} classes

URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.|tg://)[^\s<>\"']+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|me|ru|in|co|dev|app|xyz|gg|tv|ly|be|info)(?:/[^\s<>\"']*)?"
)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
MENTION_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}\b")
HASHTAG_RE = re.compile(r"(?<![\w#])#[\p{L}\p{N}_]{1,64}")
CASHTAG_RE = re.compile(r"(?<![\w$])\$[A-Z]{1,8}\b")
COMMAND_RE = re.compile(r"^/[A-Za-z0-9_]{1,32}(?:@[A-Za-z0-9_]{3,32})?(?:\s|$)")
# Digits glued to *alphabetic* words ("mp3", "A4") aren't numbers; digits next to CJK/Hangul/Thai characters are
# ("2枚", "2张", "10시"): those scripts don't put spaces around numbers.
_ALPHA = r"\p{Latin}\p{Cyrillic}\p{Greek}\p{Armenian}\p{Georgian}"
NUMBER_RE = re.compile(rf"(?<![{_ALPHA}])[-+]?\d[\d.,:/]*\d%?|(?<![{_ALPHA}\d])\d%?(?![{_ALPHA}\d])")
EMOJI_RE = re.compile(r"[\p{Extended_Pictographic}\p{Emoji_Modifier}\p{Regional_Indicator}‍️⃣]+")
CODE_FENCE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.S)
WORD_RE = re.compile(r"[\p{L}\p{M}']+")
_SHORT_LAUGHS = frozenset({"xd", "kk", "ww", "ㅋㅋ", "ㅎㅎ", "草草"})
LAUGH_RE = re.compile(
    r"(?i)^(?:(?:ha|he|hi|ja|je|ji|ka|kk|k|xa|xd|lo+l|lmf?a+o+|rofl|jk|w+|ㅋ+|ㅎ+|哈+|笑+|草+|ххх+|ахах[ах]*|хах[ах]*|5+|"
    r"wk+|awk+|kkk+|rsrs+|mdr+|jsjs+)+)$"
)


class ContentKind(Enum):
    EMPTY = "empty"
    COMMAND = "command"
    EMOJI_ONLY = "emoji_only"
    URL_ONLY = "url_only"
    MENTION_ONLY = "mention_only"
    NUMERIC = "numeric"  # numbers, math, prices, times
    CODE = "code"
    LAUGHTER = "laughter"  # "hahaha", "ㅋㅋㅋ", "jajaja" alone: universal, no translation value
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class TextProfile:
    kind: ContentKind
    core: str  # linguistic content with URLs/mentions/emoji/numbers removed
    words: tuple[str, ...]
    letters: int


_MATH_CHARS = set("0123456789+-*/=^().,%:×÷<>≤≥≈ $€£¥₹")


def _looks_like_code(text: str) -> bool:
    if CODE_FENCE_RE.fullmatch(text.strip()):
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    codeish = 0
    for ln in lines:
        s = ln.strip()
        sym = sum(1 for c in s if c in "{}[]();=<>$_\\|&")
        # A trailing ";" alone is not code: it is the Greek question mark (and common in prose).
        if (
            (s.endswith((";", "{", "}", "):")) and any(c in s for c in "=(){}[]<>"))
            or re.match(r"^(def |class |import |from \S+ import|function |const |let |var |return |SELECT |#include)", s)
            or (len(s) > 8 and sym / len(s) > 0.18)
        ):
            codeish += 1
    return codeish / len(lines) >= 0.6


def strip_non_linguistic(text: str) -> str:
    s = CODE_FENCE_RE.sub(" ", text)
    for rx in (EMAIL_RE, URL_RE, MENTION_RE, HASHTAG_RE, CASHTAG_RE, EMOJI_RE, NUMBER_RE):
        s = rx.sub(" ", s)
    return str(re.sub(r"\s+", " ", s)).strip()


def profile_text(text: str | None) -> TextProfile:
    raw = (text or "").strip()
    if not raw:
        return TextProfile(ContentKind.EMPTY, "", (), 0)
    if COMMAND_RE.match(raw):
        return TextProfile(ContentKind.COMMAND, "", (), 0)
    if _looks_like_code(raw):
        return TextProfile(ContentKind.CODE, "", (), 0)
    core = strip_non_linguistic(raw)
    words = tuple(WORD_RE.findall(core))
    letters = sum(len(w) for w in words)
    if letters == 0:
        stripped = EMOJI_RE.sub("", raw).strip()
        if not stripped or all(not unicodedata.category(c).startswith(("L", "N")) for c in stripped):
            if EMOJI_RE.search(raw) and not URL_RE.search(raw) and not MENTION_RE.search(raw):
                return TextProfile(ContentKind.EMOJI_ONLY, "", (), 0)
        if URL_RE.search(raw) or EMAIL_RE.search(raw):
            return TextProfile(ContentKind.URL_ONLY, "", (), 0)
        if MENTION_RE.search(raw) or HASHTAG_RE.search(raw):
            return TextProfile(ContentKind.MENTION_ONLY, "", (), 0)
        if all(c in _MATH_CHARS or c.isspace() or not c.isalpha() for c in raw):
            return TextProfile(ContentKind.NUMERIC, "", (), 0)
        return TextProfile(ContentKind.EMOJI_ONLY, "", (), 0)
    joined = "".join(words)
    # one short syllable ("Ja" = yes, "he", "k") is a word, not laughter; "jaja", "xD", "kk", "ㅋㅋ" are
    if len(words) <= 3 and LAUGH_RE.match(joined) and (len(joined) >= 3 or joined.lower() in _SHORT_LAUGHS):
        return TextProfile(ContentKind.LAUGHTER, core, words, letters)
    return TextProfile(ContentKind.TEXT, core, words, letters)


# --- Script analysis ---------------------------------------------------------------------------

_SCRIPT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Hangul", re.compile(r"\p{Hangul}")),
    ("Kana", re.compile(r"[\p{Hiragana}\p{Katakana}]")),
    ("Han", re.compile(r"\p{Han}")),
    ("Cyrillic", re.compile(r"\p{Cyrillic}")),
    ("Arabic", re.compile(r"\p{Arabic}")),
    ("Hebrew", re.compile(r"\p{Hebrew}")),
    ("Devanagari", re.compile(r"\p{Devanagari}")),
    ("Bengali", re.compile(r"\p{Bengali}")),
    ("Gurmukhi", re.compile(r"\p{Gurmukhi}")),
    ("Gujarati", re.compile(r"\p{Gujarati}")),
    ("Tamil", re.compile(r"\p{Tamil}")),
    ("Telugu", re.compile(r"\p{Telugu}")),
    ("Kannada", re.compile(r"\p{Kannada}")),
    ("Malayalam", re.compile(r"\p{Malayalam}")),
    ("Sinhala", re.compile(r"\p{Sinhala}")),
    ("Thai", re.compile(r"\p{Thai}")),
    ("Lao", re.compile(r"\p{Lao}")),
    ("Khmer", re.compile(r"\p{Khmer}")),
    ("Myanmar", re.compile(r"\p{Myanmar}")),
    ("Greek", re.compile(r"\p{Greek}")),
    ("Armenian", re.compile(r"\p{Armenian}")),
    ("Georgian", re.compile(r"\p{Georgian}")),
    ("Ethiopic", re.compile(r"\p{Ethiopic}")),
    ("Latin", re.compile(r"\p{Latin}")),
)


def script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, rx in _SCRIPT_PATTERNS:
        n = len(rx.findall(text))
        if n:
            counts[name] = n
    # Japanese text mixes Han + Kana; attribute Han to Kana if any kana present.
    if "Kana" in counts and "Han" in counts:
        counts["Kana"] += counts.pop("Han")
    return counts


# --- Entities that must survive translation ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtectedEntities:
    urls: tuple[str, ...]
    mentions: tuple[str, ...]
    hashtags: tuple[str, ...]
    numbers: tuple[str, ...]
    emails: tuple[str, ...]


TIME_RE = re.compile(r"(\d{1,2})[:.h](\d{2})")
CJK_TIME_RE = re.compile(r"(\d{1,2})\s*[時时点點시]\s*(?:(\d{1,2})\s*[分분])?")
_THAI_RE = re.compile(r"\p{Thai}")
AMPM_RE = re.compile(r"(?i)\b(\d{1,2})\s*(?:a\.?m\.?|p\.?m\.?)")


def _digits(n: str) -> str:
    """ASCII digit sequence; native digits ("۳", "१२", "٣") map to their values so 3 == ۳."""
    return "".join(str(unicodedata.decimal(c)) for c in n if unicodedata.decimal(c, None) is not None)


def extract_entities(text: str) -> ProtectedEntities:
    plain = html_to_plain(text) if "<" in text else text
    emails = tuple(EMAIL_RE.findall(plain))  # emails first: their domain part would match URL_RE
    no_emails = EMAIL_RE.sub(" ", plain)
    urls = tuple(URL_RE.findall(no_emails))
    no_urls = URL_RE.sub(" ", no_emails)
    return ProtectedEntities(
        urls=urls,
        mentions=tuple(MENTION_RE.findall(no_urls)),
        hashtags=tuple(HASHTAG_RE.findall(no_urls)),
        numbers=tuple(n for n in NUMBER_RE.findall(no_urls) if _digits(n)),
        emails=emails,
    )


def missing_entities(source: str, translation: str) -> list[str]:
    """Entities present in the source that disappeared from the translation (cheap hallucination guard).

    Numbers are compared by digit sequence so locale formatting changes (1,000 -> 1.000; 7:30 -> 7:30)
    are tolerated, but changed values (7 -> 8) are flagged. Hashtags are allowed to be translated.
    """
    src = extract_entities(source)
    out_plain = html_to_plain(translation) if "<" in translation else translation
    missing: list[str] = []
    for u in src.urls:
        if u.rstrip(".,!?)") not in out_plain:
            missing.append(u)
    for m in (*src.mentions, *src.emails):
        if m.lower() not in out_plain.lower():
            missing.append(m)
    out_digits = [_digits(n) for n in extract_entities(out_plain).numbers]
    pool = list(out_digits)
    out_times = {(int(h) % 12, mm) for h, mm in TIME_RE.findall(out_plain)}
    out_times |= {(int(h) % 12, "00") for h in AMPM_RE.findall(out_plain)}
    # CJK/Korean clock formats: 8時30分 / 8点30分 / 8시 30분 / 午後5時
    out_times |= {(int(h) % 12, (mm or "00").zfill(2)) for h, mm in CJK_TIME_RE.findall(out_plain)}
    thai = bool(_THAI_RE.search(source))
    for n in src.numbers:
        d = _digits(n)
        if thai and re.fullmatch(r"5{3,}", d):
            continue  # Thai laughter "555" ("ha ha ha") is meant to be translated, not kept
        t = TIME_RE.fullmatch(n)
        if d in pool:
            pool.remove(d)
        elif t and (int(t.group(1)) % 12, t.group(2)) in out_times:
            continue  # 18:30 -> 6:30 p.m. (and 18:00 -> 6 pm) are the same time
        elif re.fullmatch(r"1[3-9]|2[0-3]", d) and ((int(d) % 12, "00") in out_times or str(int(d) - 12) in pool):
            # bare 24-hour clock: "finisco alle 19" -> "done at 7 p.m.", "alle 21?" -> "at 9?" (benchmark 2026-11-22)
            if str(int(d) - 12) in pool:
                pool.remove(str(int(d) - 12))
            continue
        elif not any(d in od for od in out_digits):
            missing.append(n)
    return missing


# --- Telegram HTML sanitizing --------------------------------------------------------------------

_ALLOWED = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote",
    "span",
}
# <tg-emoji> is dropped (its fallback emoji text is kept): bots may only send custom emoji if the bot owner
# has Premium, and an unsupported tag would make the whole send fail.
_ALLOWED_ATTRS = {"a": {"href"}, "span": {"class"}, "pre": set(), "code": {"class"}, "blockquote": {"expandable"}}
_SAFE_HREF = re.compile(r"(?i)^(https?://|tg://|mailto:)")


def _first_href(attrs: list[tuple[str, str | None]]) -> str | None:
    """The href a browser/Telegram would use: the first one. Later duplicates are ignored."""
    for k, v in attrs:
        if k == "href":
            return v.strip() if v is not None else None
    return None


class _Sanitizer(HTMLParser):
    def __init__(self, allowed_hrefs: frozenset[str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.allowed_hrefs = allowed_hrefs  # None = any safe href; otherwise only these

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED:
            return
        if tag == "a":
            href = _first_href(attrs)
            if href is None or not _SAFE_HREF.match(href):
                return  # link dropped, its text is kept
            if self.allowed_hrefs is not None and href not in self.allowed_hrefs:
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
            self.stack.append(tag)
            return
        kept = []
        for k, v in attrs:
            if k not in _ALLOWED_ATTRS.get(tag, set()):
                continue
            if tag == "span" and v != "tg-spoiler":
                continue
            kept.append(f' {k}="{html.escape(v or "", quote=True)}"' if v is not None else f" {k}")
        if tag == "span" and not kept:
            return
        self.out.append(f"<{tag}{''.join(kept)}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            # close any unclosed inner tags to keep nesting valid
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(text: str) -> str:
    """Whitelist Telegram-supported tags; escape everything else. Output is safe for parse_mode=HTML."""
    p = _Sanitizer()
    p.feed(text)
    p.close()
    return p.result()


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        href = _first_href(attrs) if tag == "a" else None
        if href:
            self.hrefs.add(href)


def restrict_links(translation_html: str, source_html: str) -> str:
    """Sanitize, keeping a link only when the same href is in the source (link spoofing via prompt injection:
    "translate: <a href=evil>your bank</a>"). Link text is kept. Both sides are parsed by the same HTML parser
    the sanitizer uses, so attribute tricks (href inside another attribute, duplicate hrefs) can't diverge."""
    c = _HrefCollector()
    c.feed(source_html)
    c.close()
    p = _Sanitizer(frozenset(c.hrefs))
    p.feed(translation_html)
    p.close()
    return p.result()


class _Plain(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_plain(text: str) -> str:
    p = _Plain()
    p.feed(text)
    p.close()
    return "".join(p.parts)


def wrong_script(text: str, target_script: str) -> bool:
    """True when a translation into a non-Latin-script language is dominated by another non-Latin script
    (observed live: a Hindi translation came back in Hebrew letters). Latin letters are tolerated
    (names, URLs, brand words), as are outputs with too few letters to judge."""
    if target_script == "Latin":
        return False
    counts = script_counts(strip_non_linguistic(text))
    counts.pop("Latin", None)
    if target_script == "Kana":  # Japanese mixes kana and kanji
        counts["Kana"] = counts.get("Kana", 0) + counts.pop("Han", 0)
    total = sum(counts.values())
    if total < 2:
        return False
    return counts.get(target_script, 0) / total < 0.5


_LATIN_WORD_RE = re.compile(r"[\p{Latin}][\p{Latin}\p{M}']+")


# Languages routinely and correctly written in Latin as well as their table script (Serbian: both official;
# Kazakh: moving to Latin). Latin output for them is not "romanized".
_DIGRAPHIC = frozenset({"sr", "kk"})


def romanized_output(source: str, translation: str, target_script: str, target: str | None = None) -> bool:
    """True when a translation into a non-Latin-script language came back transliterated into Latin letters
    (observed live: "LOL TERA MAZAK CHAL RAHA HAI?" and "apni poori koshish karo!" for Hindi readers).
    `wrong_script` tolerates Latin on purpose (names, URLs, brand words), so this checks separately: several
    Latin words that were not in the source, and Latin letters outnumbering the target script's letters."""
    if target_script == "Latin" or (target is not None and target.split("-")[0].lower() in _DIGRAPHIC):
        return False
    src_words = {w.lower() for w in _LATIN_WORD_RE.findall(source)}
    plain = strip_non_linguistic(translation)
    new_latin = [w for w in _LATIN_WORD_RE.findall(plain) if w.lower() not in src_words]
    if len(new_latin) < 3:
        return False
    counts = script_counts(plain)
    if target_script == "Kana":
        counts["Kana"] = counts.get("Kana", 0) + counts.pop("Han", 0)
    native = counts.get(target_script, 0)
    return sum(len(w) for w in new_latin) > 2 * native


# Non-Latin-script targets wordfreq can score with plain word splitting (no MeCab/jieba needed).
_GARBLE_LANGS = frozenset({"ar", "bg", "bn", "el", "fa", "he", "hi", "mk", "ru", "ta", "uk", "ur"})
_LATIN_TOKEN_RE = re.compile(r"^[\p{Latin}\p{M}']+$")


def looks_garbled(source: str, translation: str, target: str) -> bool:
    """True when a translation into a non-Latin-script language is mostly non-words.

    Observed live (2026-11-21): on slangy input, gemini-3.5-flash-lite sometimes emits word salad such as
    "Жестче азец гага Чтев деет завтра?" — right script, so `wrong_script` passes. Fails open (False) for
    unsupported languages, short outputs, and any lookup error. Tokens copied from the source (names), Latin
    tokens, laughter and letter runs ("кккк", "هههه") are not judged.
    """
    if target not in _GARBLE_LANGS:
        return False
    try:
        from wordfreq import zipf_frequency

        src_words = {w.lower() for w in WORD_RE.findall(source)}
        words = []
        for w in WORD_RE.findall(strip_non_linguistic(translation)):
            lw = w.lower().strip("'")
            if (not lw or lw in src_words or _LATIN_TOKEN_RE.match(lw) or LAUGH_RE.match(lw)
                    or len(set(lw)) <= 2):
                continue
            words.append(lw)
        if len(words) < 4:
            return False
        # always the full list (lazily, target language only): "small" lists omit ordinary inflected forms
        unknown = sum(zipf_frequency(w, target, wordlist="best") < 1.0 for w in words)
    except Exception:  # a validation helper must never break delivery
        return False
    return unknown >= 2 and unknown / len(words) > 0.3


def normalize_for_key(text: str) -> str:
    """Cache-key normalization: NFC, collapse runs of spaces/tabs, keep line breaks (translations preserve
    them, so text split over two lines must not share a cached result with the one-line version). Case and
    punctuation are kept (they carry tone)."""
    s = unicodedata.normalize("NFC", text).replace("\r\n", "\n")
    s = re.sub(r"[^\S\n]+", " ", s)
    return str(re.sub(r" *\n *", "\n", s)).strip()


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"
