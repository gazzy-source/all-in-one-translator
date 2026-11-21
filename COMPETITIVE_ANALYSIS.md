# Competitive analysis

Research date: 2026-11-21. Sources are public docs/pages/repos (listed at the end). **t.me bot pages could not be
fetched from the research machine**, so bot descriptions and user counts come from third-party pages; anything
not verifiable is marked *unverified*. "Our implementation" describes code in this repo; the
**Status** column says whether it is *mock-tested* (automated tests with fake Telegram/providers) or
*live-verified* (exercised against real Telegram/providers — see `PLAN.md` → Live verification log).

## Competitors (what they actually do)

| Product | Verified behaviour | Gaps relevant to us |
|---|---|---|
| **Polyglot** (@ChatPolyglotBot, madbots.dev) | Auto-translates group messages, posting a **public** translation under each message with a "Portuguese → English" header; one target language per group/topic (61 languages, mini-app config); per-topic language/on-off; translates captions on photos/videos/GIFs; skips commands, bot messages, channel forwards. Replying to a translation makes the bot post the reply translated and **delete the replier's original and its own earlier translation**; deletes its translation if the original is deleted within ~10 min. Requires admin with post/edit/**delete** rights. AI engine with Google Translate backup. Free. | Every translation is public clutter; one language per group; deletes members' messages; no sticker/GIF-image text; no personal languages found; inline mode *unverified*. |
| **Telegram built-in translation** | Free: translate a single message on demand (hold → Translate). Premium: real-time whole-chat translation bar; channel owners can auto-translate for readers; 2026 AI editor translates your own drafts. Engine: Telegram "Cocoon" (Telegram says no data to third parties; a bug-tracker report says it sometimes adds words). | Real-time chat translation needs **Premium for every reader**; no translation of text inside stickers/GIFs/images *(unverified)*; no group-shared terminology/context controls. Client support per platform *unverified*. |
| **Yandex Translate** (@YTranslateBot) | Private chat: `/tolang` sets target(s); group: any member can add it; translates "into the languages of chat participants"; `/mylang`, `/setmylang` personal language; language auto-off 30 days after its last speaker leaves. | Group posting format, automatic vs on-demand, inline, OCR, limits *undocumented/unverified*. |
| **TgTranslatorBot** (open source, C#, 118★) | Modes: Auto (non-main-language messages), Forwards-only, Manual (`!translate` / mention); 105 languages (Google Translate set), pluggable engine; character limit; group timeouts. | Reply vs edit posting *unverified*; no personal languages; no OCR. |
| **TransChat** (@transchat_bot) | Catalogue listing only: "AI" background group translator with preferred languages. | Everything else *unverified*. |
| **Kite** (@UseKiteBot) | *Nothing verifiable* (no web/GitHub/catalogue results; t.me unreachable). | — |
| Open-source misc. (deseven/telegram-groupchat-translator 3★, soos3d/ai-tg-translator-bot 2★, lexsysko/translator_404_bot 1★, PasaOpasen/TranslatorBot 6★) | OpenAI-compatible/Groq/Google engines; per-user whitelists with target language (deseven); offline detection then Google (lexsysko); translate into all chat languages at once (PasaOpasen). All post publicly. | Public clutter; no media OCR; minimal reliability engineering. |

**No competitor found uses ephemeral (recipient-only) messages** (Bot API 10.2, July 2026).

## Feature comparison

| Area | Best competitor behaviour | Our implementation | Our advantage | Status |
|---|---|---|---|---|
| Group translation | Polyglot: public reply per message | Private **ephemeral** translation per approved recipient; nothing posted publicly; `PublicOutputGuard` blocks any public API call | Group looks unchanged to everyone else | mock-tested |
| Group clutter | Polyglot deletes messages to reduce clutter | Zero bot messages in the group; no group command menu; no reactions/typing/pins/edits/deletes | Non-interference by construction | mock-tested |
| Language detection | Provider detection (Google) | Local gate: script + wordfreq + lingua + Hinglish/Arabizi/slang lexicons; LLM decides only uncertain cases (6.8% of corpus) | Correct on short slang ("no manches", "yaar kya scene hai"); no wasted calls | mock-tested (offline corpus, 103 cases: 0% false-translate, 0% missed) |
| Personal languages | Yandex `/setmylang`; deseven whitelist | Per-user language persisted by Telegram ID; each recipient translated separately (once per language) | True per-person delivery without spamming others | mock-tested |
| Inline translation | *unverified* for competitors | `@bot text` → translated preview; tap sends the real translation; debounced; cached; approved-only by default | Fast, clean preview | mock-tested |
| OCR (images) | None verified | RapidOCR text gate + Gemini vision; caption and image text combined | Reads meme/screenshot text | mock-tested (local OCR on synthetic fixtures) |
| Stickers | Polyglot explicitly doesn't | Static WEBP OCR + vision; cache by `file_unique_id` | Unique | mock-tested |
| Animated stickers (TGS) | None | Safe gzip/Lottie parse → rlottie frames → distinct-frame OCR | Unique | mock-tested (textless TGS only; text-bearing TGS fixtures not yet built) |
| GIFs / video stickers | Polyglot: caption only | ffmpeg frame extraction (sandboxed args, time/frame caps) → changed-pixel frame selection → ≤4 text frames to vision | Animated text "WAIT / WHAT / IS THIS" read once, in order | mock-tested (synthetic MP4/WEBM) |
| Context awareness | *unverified* | Pseudonymous last-6-message window + replied-to/quoted text, only for short/ambiguous/reply messages | "ni modo" after a question → "oh well" | mock-tested (prompting); quality *pending live benchmark* |
| Reply handling | Polyglot deletes + reposts | Translations are delivered as ephemeral replies to the original; reply context feeds translation | No message deletion ever | mock-tested |
| Formatting | Header + text | Telegram-HTML preserved and re-sanitized; URLs/mentions/numbers validated; DeepL protected spans | Entities can't silently change | mock-tested |
| Speed | *unverified* | Async lanes; inline 4 s deadline; text never waits behind media | — | *pending live measurement* |
| Privacy | Polyglot requires delete rights | Admin with **no** rights; no message text stored or logged; photos never persisted; `/forgetme` | Minimal permissions | mock-tested |
| Cost | Free (competitors' own infra) | Free tiers only (Gemini + DeepL Free) with local quota guards | Self-hostable at $0 | design |
| Reliability | *unverified* | Bounded queues, dedupe, breakers, 1 retry + 1 fallback hop, graceful shutdown, owner failure digest | — | mock-tested |
| Topic support | Polyglot: per-topic language/on-off | Per-topic enable/disable; topic-scoped context and delivery | Partial parity (no per-topic language — per-person languages instead) | mock-tested |
| Configuration | Polyglot mini-app | Owner DM keyboards (`/chats`, `/recipients`), personal `/settings`, hidden per-owner group commands | Nothing visible in the group | mock-tested |

## Where we are weaker / unproven

* **Telegram built-in translation** needs no bot at all and Telegram processes it on its own infrastructure; ours
  sends text to Google/DeepL.
* **Ephemeral delivery is best-effort by Telegram's own docs** (may not arrive if offline, may disappear after
  restart). Polyglot's public messages are persistent and visible to late joiners.
* We support approved recipients only (by design); Polyglot serves everyone in the group automatically.
* Translation quality claims are **unproven until the live benchmark runs** (`benchmarks/bench_translation.py`).

## Sources

* Telegram Bot API & changelog: https://core.telegram.org/bots/api , https://core.telegram.org/bots/api-changelog ,
  https://core.telegram.org/bots/features , https://core.telegram.org/bots/faq , https://core.telegram.org/api/links
* Polyglot: https://madbots.dev/polyglot/
* Yandex: https://yandex.com/support/translate/bot.html
* Telegram translation: https://telegram.org/faq , https://telegram.org/blog/profile-pics-emoji-translations
* TgTranslatorBot: https://github.com/Dubzer/TgTranslator
* TransChat listings: https://www.findmini.app/transchat_bot/ , https://minitelegram.com/en/apps/transchat_bot
* Others: https://github.com/deseven/telegram-groupchat-translator , https://github.com/soos3d/ai-tg-translator-bot ,
  https://github.com/lexsysko/translator_404_bot , https://github.com/PasaOpasen/TranslatorBot ,
  https://botostore.com/c/translationchatbot/ , https://translatebot.io/
