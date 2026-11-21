"""Language registry: ISO 639-1 codes, human names, native names, scripts and lookup by name/alias."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Language:
    code: str  # ISO 639-1 (or BCP-47-ish for zh-Hant)
    name: str  # English name
    native: str
    script: str  # dominant script: Latin, Cyrillic, Arabic, Devanagari, Han, Hangul, Kana, ...
    flag: str = ""  # decorative only; never the sole identifier
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return self.name if self.native == self.name else f"{self.name} ({self.native})"


_LANGS: tuple[Language, ...] = (
    Language("en", "English", "English", "Latin", "🇬🇧", ("eng", "inglés", "anglais")),
    Language("es", "Spanish", "Español", "Latin", "🇪🇸", ("spa", "castellano", "espanol")),
    Language("fr", "French", "Français", "Latin", "🇫🇷", ("fre", "fra", "francais")),
    Language("de", "German", "Deutsch", "Latin", "🇩🇪", ("ger", "deu")),
    Language("it", "Italian", "Italiano", "Latin", "🇮🇹", ("ita",)),
    Language("pt", "Portuguese", "Português", "Latin", "🇵🇹", ("por", "portugues", "brazilian")),
    Language("nl", "Dutch", "Nederlands", "Latin", "🇳🇱", ("nld", "flemish")),
    Language("pl", "Polish", "Polski", "Latin", "🇵🇱", ("pol",)),
    Language("ro", "Romanian", "Română", "Latin", "🇷🇴", ("ron", "rum")),
    Language("cs", "Czech", "Čeština", "Latin", "🇨🇿", ("ces", "cze")),
    Language("sk", "Slovak", "Slovenčina", "Latin", "🇸🇰", ("slk",)),
    Language("sl", "Slovenian", "Slovenščina", "Latin", "🇸🇮", ("slv",)),
    Language("hr", "Croatian", "Hrvatski", "Latin", "🇭🇷", ("hrv",)),
    Language("bs", "Bosnian", "Bosanski", "Latin", "🇧🇦", ("bos",)),
    Language("sq", "Albanian", "Shqip", "Latin", "🇦🇱", ("sqi",)),
    Language("hu", "Hungarian", "Magyar", "Latin", "🇭🇺", ("hun",)),
    Language("fi", "Finnish", "Suomi", "Latin", "🇫🇮", ("fin",)),
    Language("et", "Estonian", "Eesti", "Latin", "🇪🇪", ("est",)),
    Language("lv", "Latvian", "Latviešu", "Latin", "🇱🇻", ("lav",)),
    Language("lt", "Lithuanian", "Lietuvių", "Latin", "🇱🇹", ("lit",)),
    Language("sv", "Swedish", "Svenska", "Latin", "🇸🇪", ("swe",)),
    Language("da", "Danish", "Dansk", "Latin", "🇩🇰", ("dan",)),
    Language("nb", "Norwegian", "Norsk", "Latin", "🇳🇴", ("no", "nor", "bokmal", "bokmål")),
    Language("is", "Icelandic", "Íslenska", "Latin", "🇮🇸", ("isl",)),
    Language("ga", "Irish", "Gaeilge", "Latin", "🇮🇪", ("gle",)),
    Language("cy", "Welsh", "Cymraeg", "Latin", "🏴", ("cym",)),
    Language("ca", "Catalan", "Català", "Latin", "", ("cat",)),
    Language("eu", "Basque", "Euskara", "Latin", "", ("eus",)),
    Language("gl", "Galician", "Galego", "Latin", "", ("glg",)),
    Language("tr", "Turkish", "Türkçe", "Latin", "🇹🇷", ("tur", "turkce")),
    Language("az", "Azerbaijani", "Azərbaycan", "Latin", "🇦🇿", ("aze",)),
    Language("uz", "Uzbek", "Oʻzbek", "Latin", "🇺🇿", ("uzb",)),
    Language("id", "Indonesian", "Bahasa Indonesia", "Latin", "🇮🇩", ("ind", "bahasa")),
    Language("ms", "Malay", "Bahasa Melayu", "Latin", "🇲🇾", ("msa", "may")),
    Language("tl", "Filipino", "Filipino", "Latin", "🇵🇭", ("fil", "tagalog")),
    Language("vi", "Vietnamese", "Tiếng Việt", "Latin", "🇻🇳", ("vie",)),
    Language("sw", "Swahili", "Kiswahili", "Latin", "🇰🇪", ("swa",)),
    Language("af", "Afrikaans", "Afrikaans", "Latin", "🇿🇦", ("afr",)),
    Language("yo", "Yoruba", "Yorùbá", "Latin", "🇳🇬", ("yor",)),
    Language("ha", "Hausa", "Hausa", "Latin", "🇳🇬", ("hau",)),
    Language("so", "Somali", "Soomaali", "Latin", "🇸🇴", ("som",)),
    Language("eo", "Esperanto", "Esperanto", "Latin", "", ("epo",)),
    Language("la", "Latin", "Latina", "Latin", "", ("lat",)),
    Language("ru", "Russian", "Русский", "Cyrillic", "🇷🇺", ("rus", "russkiy")),
    Language("uk", "Ukrainian", "Українська", "Cyrillic", "🇺🇦", ("ukr",)),
    Language("be", "Belarusian", "Беларуская", "Cyrillic", "🇧🇾", ("bel",)),
    Language("bg", "Bulgarian", "Български", "Cyrillic", "🇧🇬", ("bul",)),
    Language("sr", "Serbian", "Српски", "Cyrillic", "🇷🇸", ("srp",)),
    Language("mk", "Macedonian", "Македонски", "Cyrillic", "🇲🇰", ("mkd",)),
    Language("kk", "Kazakh", "Қазақ", "Cyrillic", "🇰🇿", ("kaz",)),
    Language("mn", "Mongolian", "Монгол", "Cyrillic", "🇲🇳", ("mon",)),
    Language("el", "Greek", "Ελληνικά", "Greek", "🇬🇷", ("ell", "gre")),
    Language("hy", "Armenian", "Հայերեն", "Armenian", "🇦🇲", ("hye",)),
    Language("ka", "Georgian", "ქართული", "Georgian", "🇬🇪", ("kat",)),
    Language("he", "Hebrew", "עברית", "Hebrew", "🇮🇱", ("heb", "iw")),
    Language("ar", "Arabic", "العربية", "Arabic", "🇸🇦", ("ara", "arabi")),
    Language("fa", "Persian", "فارسی", "Arabic", "🇮🇷", ("fas", "farsi")),
    Language("ur", "Urdu", "اردو", "Arabic", "🇵🇰", ("urd",)),
    Language("ps", "Pashto", "پښتو", "Arabic", "🇦🇫", ("pus",)),
    Language("hi", "Hindi", "हिन्दी", "Devanagari", "🇮🇳", ("hin", "hindustani", "hinglish")),
    Language("mr", "Marathi", "मराठी", "Devanagari", "🇮🇳", ("mar",)),
    Language("ne", "Nepali", "नेपाली", "Devanagari", "🇳🇵", ("nep",)),
    Language("bn", "Bengali", "বাংলা", "Bengali", "🇧🇩", ("ben", "bangla")),
    Language("pa", "Punjabi", "ਪੰਜਾਬੀ", "Gurmukhi", "🇮🇳", ("pan",)),
    Language("gu", "Gujarati", "ગુજરાતી", "Gujarati", "🇮🇳", ("guj",)),
    Language("ta", "Tamil", "தமிழ்", "Tamil", "🇮🇳", ("tam",)),
    Language("te", "Telugu", "తెలుగు", "Telugu", "🇮🇳", ("tel",)),
    Language("kn", "Kannada", "ಕನ್ನಡ", "Kannada", "🇮🇳", ("kan",)),
    Language("ml", "Malayalam", "മലയാളം", "Malayalam", "🇮🇳", ("mal",)),
    Language("si", "Sinhala", "සිංහල", "Sinhala", "🇱🇰", ("sin",)),
    Language("th", "Thai", "ไทย", "Thai", "🇹🇭", ("tha",)),
    Language("lo", "Lao", "ລາວ", "Lao", "🇱🇦", ("lao",)),
    Language("km", "Khmer", "ខ្មែរ", "Khmer", "🇰🇭", ("khm",)),
    Language("my", "Burmese", "မြန်မာ", "Myanmar", "🇲🇲", ("mya", "myanmar")),
    Language("am", "Amharic", "አማርኛ", "Ethiopic", "🇪🇹", ("amh",)),
    Language("zh", "Chinese (Simplified)", "简体中文", "Han", "🇨🇳", ("zho", "chi", "chinese", "mandarin", "zh-hans", "zh-cn")),
    Language("zh-Hant", "Chinese (Traditional)", "繁體中文", "Han", "🇹🇼", ("zh-tw", "zh-hk", "traditional chinese", "cantonese")),
    Language("ja", "Japanese", "日本語", "Kana", "🇯🇵", ("jpn", "jp")),
    Language("ko", "Korean", "한국어", "Hangul", "🇰🇷", ("kor", "kr")),
)

LANGUAGES: dict[str, Language] = {lang.code: lang for lang in _LANGS}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.strip().lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).replace("_", "-")


_INDEX: dict[str, str] = {}
for _l in _LANGS:
    for _key in (_l.code, _l.name, _l.native, *_l.aliases):
        _INDEX.setdefault(_norm(_key), _l.code)
_INDEX.setdefault("zh-hant", "zh-Hant")


def resolve_language(query: str | None) -> Language | None:
    """Resolve an ISO code, English name, native name or alias. Returns None if unknown (never guesses)."""
    if not query:
        return None
    key = _norm(query)
    code = _INDEX.get(key)
    if code is None and "-" in key:  # e.g. pt-br, en-us -> base language
        code = _INDEX.get(key.split("-", 1)[0])
    if code is None and key.startswith("chinese ("):
        code = "zh-Hant" if "trad" in key else "zh"
    return LANGUAGES.get(code) if code else None


def suggest_languages(query: str, limit: int = 5) -> list[Language]:
    """Prefix/substring suggestions for a mistyped language name."""
    key = _norm(query)
    if not key:
        return []
    hits: list[Language] = []
    for lang in _LANGS:
        names = [_norm(n) for n in (lang.code, lang.name, lang.native, *lang.aliases)]
        if any(n.startswith(key) for n in names) or (len(key) >= 3 and any(key in n for n in names)):
            hits.append(lang)
    return hits[:limit]


def lang_name(code: str | None) -> str:
    if not code:
        return "Unknown"
    lang = LANGUAGES.get(code) or resolve_language(code)
    return lang.name if lang else code


# Languages offered first in pickers (most common in Telegram groups).
POPULAR: tuple[str, ...] = (
    "en", "es", "hi", "ar", "ru", "pt", "fr", "de", "tr", "id", "zh", "ja", "ko", "it", "uk", "fa", "ur",
    "bn", "vi", "pl",
)


_HANT_CODES = {"zh-hant", "zh-tw", "zh-hk", "zh-mo"}


def same_language(a: str | None, b: str | None) -> bool:
    """Base-language equality, except Simplified vs Traditional Chinese, which readers treat as different
    targets ("zh" == "zh-Hans" == "zh-CN"; "zh-Hant" == "zh-TW"; but "zh" != "zh-Hant")."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    if a.split("-")[0] != b.split("-")[0]:
        return False
    if a.split("-")[0] == "zh":
        return (a in _HANT_CODES) == (b in _HANT_CODES)
    return True
