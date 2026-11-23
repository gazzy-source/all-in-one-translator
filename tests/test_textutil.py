from __future__ import annotations

import pytest

from translator_bot.textutil import (
    ContentKind,
    extract_entities,
    html_to_plain,
    missing_entities,
    profile_text,
    sanitize_html,
    script_counts,
)


@pytest.mark.parametrize(("text", "kind"), [
    ("", ContentKind.EMPTY),
    ("   ", ContentKind.EMPTY),
    ("😂😂😂", ContentKind.EMOJI_ONLY),
    ("👍", ContentKind.EMOJI_ONLY),
    ("❤️‍🔥 🇪🇸", ContentKind.EMOJI_ONLY),
    ("https://example.com", ContentKind.URL_ONLY),
    ("www.example.com/path?q=1", ContentKind.URL_ONLY),
    ("/start", ContentKind.COMMAND),
    ("/setlang@my_bot es", ContentKind.COMMAND),
    ("@someuser", ContentKind.MENTION_ONLY),
    ("#tag", ContentKind.MENTION_ONLY),
    ("100%", ContentKind.NUMERIC),
    ("12:30", ContentKind.NUMERIC),
    ("2+2=4", ContentKind.NUMERIC),
    ("$5.99", ContentKind.NUMERIC),
    ("hahaha", ContentKind.LAUGHTER),
    ("jajaja", ContentKind.LAUGHTER),
    ("ㅋㅋㅋㅋ", ContentKind.LAUGHTER),
    ("lmaooo", ContentKind.LAUGHTER),
    ("kkkkk", ContentKind.LAUGHTER),
    ("def foo(x):\n    return x + 1;", ContentKind.CODE),
    ("```print('hi')```", ContentKind.CODE),
    ("no manches 😂", ContentKind.TEXT),
    ("ça va?", ContentKind.TEXT),
    ("ㅋㅋㅋㅋ 오늘 뭐해?", ContentKind.TEXT),
    ("ok", ContentKind.TEXT),
])
def test_profile(text: str, kind: ContentKind) -> None:
    assert profile_text(text).kind is kind


def test_short_meaningful_text_not_dropped() -> None:
    for t in ("ça va?", "ni modo", "hola", "si", "да"):
        assert profile_text(t).kind is ContentKind.TEXT


def test_script_counts_japanese_mixes_han_into_kana() -> None:
    c = script_counts("今日は何をしますか")
    assert "Kana" in c and "Han" not in c


def test_entities() -> None:
    e = extract_entities("Hey @bob, price is $1,299.50 at 7:30 see https://x.io/a #deal mail me a@b.co")
    assert e.mentions == ("@bob",)
    assert e.urls == ("https://x.io/a",)
    assert e.hashtags == ("#deal",)
    assert "a@b.co" in e.emails
    assert any("1,299.50" in n for n in e.numbers) and "7:30" in e.numbers


@pytest.mark.parametrize(("source", "translation", "missing"), [
    ("Meeting at 10:00", "မနက် ၁၀ နာရီ", []),
    ("Meeting at 10:30", "၁၀ နာရီ ၃၀ မိနစ်", []),
    ("Meeting at 10:30", "၁၀ နာရီ ၃၁ မိနစ်", ["10:30"]),
    ("Meeting at 10:00", "၁၁ နာရီ", ["10:00"]),
    ("Meeting at 10:30", "၁၀ နာရီ", ["10:30"]),
    ("Price is 1000", "၁၀ နာရီ", ["1000"]),
])
def test_burmese_clock_entities(source: str, translation: str, missing: list[str]) -> None:
    assert missing_entities(source, translation) == missing


def test_missing_entities_tolerates_locale_number_format() -> None:
    src = "Costs 1,000 euros at 7:30 https://ex.com @ann"
    assert missing_entities(src, "Cuesta 1.000 euros a las 7:30 https://ex.com @ann") == []
    assert "7:30" in missing_entities(src, "Cuesta 1.000 euros a las 8:30 https://ex.com @ann")
    assert "https://ex.com" in missing_entities(src, "Cuesta 1.000 euros a las 7:30 @ann")
    assert "@ann" in missing_entities(src, "Cuesta 1.000 euros a las 7:30 https://ex.com")


@pytest.mark.parametrize(("raw", "expected"), [
    ("<b>bold</b> & <i>it</i>", "<b>bold</b> &amp; <i>it</i>"),
    ("<script>alert(1)</script>hi", "alert(1)hi"),
    ('<a href="javascript:alert(1)">x</a>', "x"),
    ('<a href="https://ok.com" onclick="x">x</a>', '<a href="https://ok.com">x</a>'),
    ("<b>unclosed", "<b>unclosed</b>"),
    ("1 < 2 > 0", "1 &lt; 2 &gt; 0"),
    ('<span class="tg-spoiler">s</span><span class="x">y</span>', '<span class="tg-spoiler">s</span>y'),
    ('<tg-emoji emoji-id="5">🔥</tg-emoji>', "🔥"),
])
def test_sanitize(raw: str, expected: str) -> None:
    assert sanitize_html(raw) == expected


def test_html_to_plain() -> None:
    assert html_to_plain("<b>a</b> &amp; <i>b</i>") == "a & b"


def test_time_format_conversion_is_not_a_missing_number() -> None:
    # observed live: DeepL rendered "18:30" as "6:30 p.m."
    assert missing_entities("nos vemos a las 18:30", "see you at 6:30 p.m.") == []
    assert missing_entities("a las 18:00", "at 6 pm") == []
    assert missing_entities("a las 18:30", "at 7:30 p.m.") == ["18:30"]


def test_wrong_script_detection() -> None:
    from translator_bot.textutil import wrong_script

    assert wrong_script("בום!", "Devanagari")  # observed live: Hebrew letters for a Hindi reader
    assert not wrong_script("धमाका!", "Devanagari")
    assert not wrong_script("भाई ये OK है https://x.io", "Devanagari")  # Latin words/URLs tolerated
    assert not wrong_script("Привет", "Latin")
    assert not wrong_script("今日は何をしますか", "Kana") and wrong_script("오늘 뭐해요 친구", "Kana")
    assert not wrong_script("!", "Arabic")


def test_garbled_output_detection() -> None:
    from translator_bot.textutil import looks_garbled

    src = "マジでやばいww 明日どうする？"
    # observed live from gemini-3.5-flash-lite: right script, mostly non-words
    assert looks_garbled(src, "Жестче азец гага Чтев деет завтра?", "ru")
    assert looks_garbled(src, "Бetста жестчестстст... Чта дедаеееааm... Завтра чта дееееааm?", "ru")
    assert not looks_garbled(src, "Это просто капец лол. Что завтра делать будем?", "ru")
    assert not looks_garbled(src, "Жесть, реально капец лол. Что завтра будем делать?", "ru")
    assert not looks_garbled("x", "सच में, बहुत मज़ा आया था lol, शनिवार को फिर से करें?", "hi")
    assert not looks_garbled("x", "جد والله مصيبة هههههه بكره وش نسوي؟", "ar")
    assert not looks_garbled("x", "почему ты вчера не пришёл? кккк ахахах", "ru")  # letter runs ignored
    # names copied from the source, Latin tokens and short outputs are never judged
    assert not looks_garbled("Zxqvork and Blimpt", "Зxqvork Blimpt Zxqvork и Blimpt", "ru")
    assert not looks_garbled("x", "Ыщр жцу", "ru")
    # fails open for targets it cannot score
    assert not looks_garbled("x", "asdf qwer zxcv uiop", "en")
    assert not looks_garbled("x", "マジでやばい", "ja")


def test_restrict_links_drops_links_not_in_source() -> None:
    from translator_bot.textutil import restrict_links

    src = 'see <a href="https://ok.example/a">docs</a>'
    out = 'mira <a href="https://ok.example/a">docs</a> y <a href="https://evil.example">tu banco</a>'
    assert restrict_links(out, src) == 'mira <a href="https://ok.example/a">docs</a> y tu banco'
    assert restrict_links('<a href="tg://user?id=1">x</a>', "hola") == "x"
    assert restrict_links("<a href='https://evil.example'>unclosed", "hola") == "unclosed"
    assert restrict_links("<b>bold</b> stays", "<b>x</b>") == "<b>bold</b> stays"


def test_restrict_links_resists_attribute_tricks() -> None:
    from translator_bot.textutil import restrict_links, sanitize_html

    src = '<a href="https://ok.example">docs</a>'
    # allowed href hidden inside another attribute; the real href is evil
    assert restrict_links("""<a title='href="https://ok.example"' href="https://evil.example">x</a>""", src) == "x"
    # duplicate hrefs: only the first counts, and only one is ever emitted
    assert restrict_links('<a href="https://evil.example" href="https://ok.example">x</a>', src) == "x"
    assert (restrict_links('<a href="https://ok.example" href="https://evil.example">x</a>', src)
            == '<a href="https://ok.example">x</a>')
    assert sanitize_html('<a href="https://a.example" href="https://b.example">x</a>') == (
        '<a href="https://a.example">x</a>')
    # an evil href in the source's text (not a tag) doesn't whitelist it
    assert restrict_links('<a href="https://evil.example">x</a>', 'href="https://evil.example"') == "x"


def test_romanized_output_detection() -> None:
    from translator_bot.textutil import romanized_output

    # observed live from the vision model for Hindi readers
    assert romanized_output("MDR T'ES SÉRIEUX ?", "LOL TERA MAZAK CHAL RAHA HAI?", "Devanagari")
    assert romanized_output("がんばって", "apni poori koshish karo!", "Devanagari")
    assert romanized_output("hola amigos", "privet druzya kak dela", "Cyrillic")
    # native script with a few Latin names/brands/URLs is fine
    assert not romanized_output("check https://x.io and ping @dave before 5",
                                "https://x.io चेक करें और 5 बजे से पहले @dave को ping करें", "Devanagari")
    assert not romanized_output("Send it on WhatsApp", "WhatsApp पर भेज दो", "Devanagari")
    assert not romanized_output("Maria and Dave Smith", "Maria और Dave Smith", "Devanagari")  # copied names
    assert not romanized_output("x", "anything goes here in latin", "Latin")
    assert not romanized_output("x", "lol ok", "Devanagari")  # too short to judge
