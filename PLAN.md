# Plan & status

Legend: ✅ done & mock-tested · 🟢 live-verified · ⏳ pending live verification (needs credentials/accounts) ·
❌ not done.

## Phases

| Phase | Status |
|---|---|
| 1 Research & audit (repo was empty; Telegram Bot API 10.4; competitors; Gemini/DeepL docs; OCR options) | ✅ |
| 2 Architecture (ARCHITECTURE.md) | ✅ |
| 3 Core private group translator | ✅ mock · ⏳ live |
| 4 Inline translator | ✅ mock · ⏳ live |
| 5 Media (captions, images, stickers, TGS, video stickers, GIFs) | ✅ mock (real ffmpeg/rlottie/RapidOCR on synthetic fixtures) · ⏳ live vision |
| 6 Intelligence (context, glossary, mixed/romanized, routing, validation) | ✅ mock · ⏳ live quality |
| 7 Reliability (queues, breakers, fallbacks, shutdown, metrics, health) | ✅ mock |
| 8 Testing (unit, e2e mocked, detection benchmark, security tools) | ✅ · ⏳ live benchmark & 3-account test |
| 9 Optimization (measure latency/quota live) | ⏳ |
| 10 Finalization (docs, deployment) | ✅ |

## Decisions log

* **Python + aiogram 3.33** (Bot API 10.4, ephemeral messages supported) — best OCR/media ecosystem.
* **Delivery = ephemeral only**, never public; owner-managed recipients (default owners only) — per user's
  non-interference requirements. No group-scope command menu (would be visible to members).
* **Providers = Gemini free + DeepL Free** — user ruled out paid/Anthropic; DeepL alone can't do Hinglish/slang/
  sticker text, so Gemini (user-approved) handles those.
* **Gemini `generateContent`** (documented "legacy but supported") over the newer Interactions API whose docs
  were incomplete (no error envelope/retry info, unclear default retention).
* **Detection**: lingua alone failed on short chat; wordfreq evidence + lexicons → 0% false-translate on corpus.
* **SQLite**, in-process queues — single instance; no Redis/Celery.
* **Burst coalescing** 700 ms — fewer free-tier calls and fewer ephemeral sends.

## Live verification log

| # | Check | Result |
|---|---|---|
| L1 | Bot token valid (`getMe`) — inline mode on, privacy mode on | 🟢 2026-11-21 |
| L2 | Owner ID captured via private `/start`; Gemini + DeepL keys present | 🟢 2026-11-21 |
| L3 | Ephemeral send to a member as admin bot; edit in place — API returned `message_id=0`, `ephemeral_message_id`, edit ok. *Visual confirmation by owner pending* | 🟢 API · ⏳ visual |
| L4 | Real error texts: non-member → `Bad Request: USER_NOT_PARTICIPANT` (→ not_member ✓); missing reply target → `message to be replied not found` (→ resend without reply ✓). Non-admin case not yet observed | 🟢 (2 of 3) |
| L5 | Hidden owner `/approve` as a reply in the live group worked (approved the Hindi recipient); owner reported correct behaviour | 🟢 |
| L6 | Gemini fast/quality/vision succeed with the exact request shape (nothing stripped; `store:false` accepted). **Latency finding**: on the free tier gemini-3.6-flash timed out (40 s), 3.5-flash took 10–39 s, 3.7/3.8-flash returned 503; 3.5-flash-lite answered in 0.6–1.4 s → defaults changed to 3.5-flash-lite (MINIMAL/LOW thinking) + 3.1-flash-lite failover on 503 | 🟢 |
| L7 | DeepL Free: HI is a target; limit is 1,000,000 chars/month (not 500k); ~300–750 ms; URLs/mentions preserved via translate=no spans; 18:30→"6:30 p.m." (validation fixed to accept) | 🟢 |
| L8 | Live translation benchmark, 47 cases, auto route. Run 1: chrF 70.0, 2 silent misses, en→hi came out **romanized** (prompt bug), Hinglish gate miss. After fixes, run 2: **chrF 77.4, silent-miss 0%, source-lang acc 100%, false-translation 0%, entity/keep violations 0%, errors 0, p50 776 ms**; 21 DeepL / 21 Gemini (2 served by failover model). Remaining weakness: OCR meme "NO WAY BRO"→es varies ("NO MAMES HERMANO", "NO MÁS, BRO") | 🟢 |
| L9 | Three-account acceptance test: English recipient (owner), Hindi recipient (approved via hidden `/approve`), unapproved member. Owner got English, Hindi reader got Hindi, own messages not echoed, unapproved member and group saw nothing — confirmed by the owner on real devices | 🟢 2026-11-21 |
| L11 | Live media benchmark (23 items: stickers in 6 scripts, low-res, rotated, meme, GIF sequence, brief flash, WEBM video sticker, vector-text TGS, video with speech, voice): 100% recall, 0 false positives, 0 vision calls on textless, 100% right target language after wrong-script guard | 🟢 |
| L12 | Real sticker from a member (Burmese text) read and translated into English and Hindi in the live group | 🟢 |
| L13 | Slang into ru/ar/hi regressed after 0.2.0 routed laughter/slang to Gemini (matrix run 20:11: `Жестче азец гага Чтев деет завтра?`). Prompt A/B ruled out the prompt (old prompt garbled too). Model A/B, 27 samples per arm: 3.5-flash-lite garbled 3/25 at t=0.2 and 5/23 at t=0; 3.1-flash-lite 0/23 → text defaults switched to 3.1-flash-lite (3.5 failover; vision unchanged) + `looks_garbled` validation (mostly-non-word output → escalate → DeepL). Re-run: matrix 72 pairs, right-language 100%, 0 garbled, p50 2.3 s; translation benchmark chrF 76.8 (was 77.4, within run-to-run noise), silent-miss 0%, false-translation 0%, errors 0, **p50 1155 ms (was 776 ms)**. Remaining matrix flag: DeepL en→ja rendered 17:00 as 午後5時 (a valid conversion) | 🟢 |
| L14 | Vision-model A/B (`bench_vision_ab.py`, 193 s): 20 real stickers from two public packs (Burmese captions on photos, Hinglish doge memes, English, photo stickers without text), 7 problem stickers, 6 non-English voice notes (edge-tts). Where vision answered: Burmese read + translated naturally by both; textless photo stickers silent; English stickers not echoed to English readers; Hinglish memes correct (`tu kon hai be?` → "who the hell are you?" / "ты кто вообще?"); prompt fixes worked (Tamil `machan` → "bro"/"भाई"); ja/pt voice transcribed exactly. Model verdict **inconclusive**: 3.1 better on 3 items (Burmese misread, Indonesian→ru/ar, Myanmar→ru tone), 3.5 better on 2 (3.1 gave "A / B" alternatives; missed Tamil text). ~1/3 of calls hit free-tier rate limits after a day of benchmarks (both arms ran concurrently), so vision stays on 3.5-flash-lite (recall validated in L11). | 🟢 partial |
| L15 | Held-out text corpus (48 cases written before outputs): silent-miss 0%, false-translation 0%, source-lang 97.6%, p50 0.7 s; found accentless-Vietnamese and Thai-`555` routing bugs (fixed, live-rechecked). Pair accuracy, 119 pairs (16 sources × 8 targets incl. hi/ar/ru/ja/zh): 0 silent, 0 script problems, 0 garbled; lowest 20 by back-translation chrF all correct in meaning on reading | 🟢 |
| L10 | Offline recipient: are ephemeral translations shown after reconnecting? after app restart? | ⏳ product decision depends on result |

## Measured so far (offline)

* Language gate, 103-case corpus authored for this project (not held-out): false-translate 0%, missed 0%,
  source-language accuracy 98% (ms↔id confusion), deferred-to-LLM 6.8%, ~21–34 ms/message on this laptop.
* Test suite: 370 passed (CI: ubuntu, Python 3.12 + 3.13) (all mocked; media tests use real ffmpeg/rlottie/RapidOCR on synthetic images).
* ruff, mypy --strict, bandit: clean. pip-audit: no known vulnerabilities.

## Known gaps / next steps

* Videos and voice messages longer than `MAX_MEDIA_SECONDS` (default 1 min) are skipped.
* Isolated misspellings from the LLM (e.g. one wrong letter in a Russian word) pass validation; only
  mostly-non-word outputs are caught (`looks_garbled`, non-Latin targets only).
* When vision is unavailable (rate limit/quota), the local OCR fallback reads Latin/CJK only and can misread
  look-alike scripts (Greek `ΕΛΑ ΡΕ` → `EAA PE` → a wrong Hindi translation); voice notes have no local fallback.
* Non-English voice was tested only on synthetic speech (edge-tts neural voices); English also with a noisy 8 kHz
  variant.
* PDF/document text not implemented.
* No per-topic target languages (per-person languages instead); per-topic on/off exists in storage but has no UI yet.
* Load test against real Telegram rate limits for ephemeral messages (undocumented) not done.
