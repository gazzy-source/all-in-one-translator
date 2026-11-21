# Changelog

## 1.0.0 — 2026-11-22 (production release)

### Production
* **Self-healing:** `/healthz` reports dead queue workers and an unresponsive database (it used to say OK as long
  as the process existed); a watchdog exits non-zero after two failed checks so Docker/systemd restart the bot.
* **Docker:** `HEALTHCHECK`, `init` (reaps ffmpeg children), log rotation, JSON logs, `cap_drop: ALL`, graceful
  30 s drain, health port on by default; image published to `ghcr.io/gazzy-source/all-in-one-translator` on
  version tags.
* **CI:** builds the production image and runs a native-media smoke test inside it on a read-only root FS
  (ffmpeg with forced demuxers, rlottie, RapidOCR, language gate) plus an invalid-config exit check;
  `pip-audit` dependency scan.
* **Backups:** `python -m translator_bot.backup` — online SQLite backup (safe while running), integrity-checked,
  rotated.
* **Docs:** [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Docker and systemd setups, group setup, backups, updates,
  webhook mode, free-tier limits, troubleshooting by log line.
* Every setting is documented in `.env.example` (a test enforces it); unused `FFPROBE_PATH` removed.

## 0.2.5 — 2026-11-22 (review of recent fixes)

A fresh review of the 0.2.1–0.2.4 changes found regressions introduced by them; all fixed with tests.

* Audio files sent as WAV, FLAC or AAC (and AVI videos) were silently ignored after the forced-demuxer hardening;
  they are recognized by magic bytes again (verified with real files) — still one whitelisted demuxer each.
* Traditional → Simplified Chinese cost a call and was then discarded (providers report both as "zh"); the
  detector's script verdict now decides, and Chinese script conversion always goes to Gemini (DeepL returns
  Chinese unchanged — verified live). Both directions now convert correctly.
* Serbian (and Kazakh) written in Latin is no longer rejected as "romanized".
* Caption edits on photos/videos/voice reuse the media reading kept in memory instead of re-downloading and
  re-running vision/audio each time.
* An edit waiting for its original job no longer burns its own queue timeout; a cancelled job can't leak a lock.
* Gemini's daily quota rolls over at Pacific midnight (Google's reset), not UTC.
* Cache keys keep line breaks (two layouts of the same words no longer share a translation).
* Romanized text never falls back to DeepL as a last resort when Gemini is down (DeepL returns nonsense).

## 0.2.4 — 2026-11-22 (live group test)

* Song lyrics in voice/audio messages (within the 60 s limit) are transcribed and translated, marked ♪; speech
  over background music still wins, and instrumental music stays silent (verified live).
* `REPLY_MESSAGE_ID_INVALID` (current Telegram wording) now resends without the reply link instead of failing.
* Media that produce no translation log why (too long / nothing to translate + confidence; never content), and
  `/status` lists media skips.
* Live: spoken voice notes in Spanish, Hindi, Egyptian Arabic, Russian and French-over-music transcribed and
  translated correctly into en/hi/ru/ar.

## 0.2.3 — 2026-11-21 (code review + cross-language accuracy)

Three independent read-only reviews (flows/authorization, translation core/providers, delivery/storage/media):
no authorization gap and no path to public group output; the bugs below were each reproduced or traced before
fixing, with regression tests in `tests/test_review_fixes.py`.

### Fixed — reliability
* **Queue workers could die permanently.** When a coalesced request's owner was cancelled (its queue job timed
  out), waiters inherited `CancelledError`, which the lane worker re-raised and exited on. A few collisions
  (same sticker/text in two chats) silently stopped translation while `/healthz` stayed OK. Workers now survive
  stray cancellations and waiters redo the work (orchestrator and media coalescing).
* **Circuit breaker could stay half-open forever** when its single trial ended without an outcome (local rate
  limit, quota, deadline): the provider was never used again until restart. Stale trials now expire.
* Safety blocks / truncated output for one message no longer open the provider breaker for everyone.
* A non-JSON 200 from Gemini (proxy/gateway page) is a provider error instead of an unhandled exception.
* A slow media download no longer recycles the decode pool (which killed other jobs' renders).
* Voice notes can't bypass the duration limit by declaring a short duration: larger files are trimmed.

### Fixed — edits
* Original translation and its edits are serialized per message: an edit racing the original sent a duplicate,
  possibly stale, ephemeral.
* Caption edits on photos/GIFs re-render caption *and* image text (the image text used to be dropped);
  caption edits on videos/voice are now handled.
* Editing one message of a burst after the in-memory record expired no longer erases the others' translation.
* Failed edits retry (plain text on parse errors, short 429 waits) instead of sending a duplicate.
* Supergroup migration keeps the group's enabled flag and settings when the new id's row already exists.

### Fixed — translation accuracy
* Simplified and Traditional Chinese are distinct targets (character-based variant test); they were treated as
  the same language, so no conversion happened.
* Numbers next to CJK/Hangul characters (`2枚`, `2张`, `10시`), native digits (`۳`, `१२`) and CJK clock formats
  (`8時30分`, `8点30分`, `8시 30분`) no longer count as missing — each cost 2–3 extra API calls.
* Laughter in any script (`555`, `ㅋㅋㅋ`, `хахаха`, `ههههه`), Korean `ㅠㅠ`, and Vietnamese typed without accents
  route to the LLM; DeepL left `555`/`ㅠㅠ` as-is and misread accentless Vietnamese ("hotpot" became
  "restaurant"). Thai `555` is translated instead of protected as a number; `ㅠㅠ` becomes `😭`.
* German "Ja" (yes) is no longer classified as laughter and silently skipped.

### Hardening
* Hidden `/approve`/`/revoke` in forum topics ignore the topic's creation message (could approve the topic
  creator); malformed callback data no longer raises.
* Config fails fast on an invalid `LOG_LEVEL`, a non-numeric bot id, `MEDIA_TIMEOUT_S` < 5, or an
  `EDIT_WINDOW_S` longer than delivery retention; unused `CACHE_MAX_ROWS` removed.

### Measured
* Held-out text corpus (48 new cases, 20+ languages, written before outputs): 0 silent misses, 0 false
  translations, 0 errors, source language 97.6%, p50 0.7 s.
* Pair accuracy (`bench_pair_accuracy.py`: 16 sources × 8 targets = 119 pairs, meaning checked by DeepL
  back-translation and by reading): all translated, 0 script problems, 0 garbled; every one of the 20
  lowest-scoring pairs was correct in meaning on reading (low scores = paraphrase).

## 0.2.2 — 2026-11-21 (media matrix testing)

Found by a new live media benchmark (`benchmarks/bench_media_matrix.py`: 40 items, 22 scripts rendered with real
text shaping by headless Chrome, video stickers, GIF89a/MP4 GIFs, vector TGS, clean and noisy voice notes).
First run: recall 94.7%, 0 false positives, 0 garbled, read accuracy 96%; problems below.

### Fixed
* Fade-in / slide-in video stickers with a small caption were treated as one blank frame (64×64 thumbnails
  averaged the caption away), so their text was never read (Japanese and Arabic stickers missed). Frames are
  compared at 128×128 with finer thresholds, and the last frame is always considered.
* Real GIF89a animations were rejected by the forced ffmpeg demuxer: the container is now chosen from magic
  bytes (GIF / WebM / MP4 / Ogg / MP3), still with a single whitelisted demuxer.
* Romanized output for non-Latin-script readers (e.g. `LOL TERA MAZAK CHAL RAHA HAI?` for Hindi) slipped past
  the script check, which tolerates Latin for names/URLs: new `romanized_output` check on text and media paths.
* Media translations now pass the garbled-output check too; rejected ones are redone by the text translator.
* Video on-screen text showed a raw `[screen]` tag to readers; it is now a 📺 icon.
* Prompts: Thai `555`, Indonesian `wkwk`, Japanese `草`, Arabic `ههه` and similar laughter, and slang terms of
  address (`machan`, `wey`, `kanka`), are rendered as the reader's equivalent instead of left as-is or
  transliterated (`सुपर मचान` meant "super scaffolding").

## 0.2.1 — 2026-11-21

### Fixed
* Slang/laughter messages into Russian, Arabic and Hindi sometimes came back as word salad
  (`Жестче азец гага Чтев деет завтра?`) after 0.2.0 routed them to Gemini. Cause: `gemini-3.5-flash-lite`
  (prompt and temperature ruled out by A/B). Text translation now defaults to `gemini-3.1-flash-lite` with
  3.5-flash-lite as failover; vision is unchanged.
* New `garbled` validation: a translation into a non-Latin-script language that is mostly non-words
  (wordfreq) is rejected → escalated once → falls back to DeepL; never delivered.
* Vision failover no longer disables itself when the vision model equals the fallback model.

### Security
* Link spoofing via model output: for plain-text messages (and all image/sticker/voice text) the provider's reply
  was passed through the HTML sanitizer, which keeps `<a href>` — so a prompt-injected or OCR'd
  `<a href="https://evil">your bank</a>` became a live link in a private translation. Plain sources are now
  HTML-escaped; HTML sources keep only links that were already in the original message.

* Media: ffmpeg's demuxer is forced and whitelisted per expected container, so a file disguised as a video
  (e.g. an HLS playlist or concat list) is never parsed — defence in depth, since recent ffmpeg already refuses
  HLS with non-standard extensions. MP4 external data references explicitly disabled.
* Animated (TGS) stickers with image layers/assets or font files are rejected: rlottie would open local paths.
* Edits now pass the same gates as new messages (disabled topics, length cap), and the edit window is measured
  from when the message was sent (it was compared with the edit time, so it never applied).

### Project
* Repository renamed to `all-in-one-translator`; package renamed to `translator_bot` (default database
  `data/translator.sqlite3`); NOTICE, CI (ruff, mypy, bandit, pytest on
  3.12/3.13), issue/PR templates, CONTRIBUTING, CODE_OF_CONDUCT, AGENTS.md, `.gitattributes`, `.dockerignore`.

## 0.2.0 — 2026-11-21 (after live testing)

### Added
* **Videos & video notes (≤ 1 min) and voice messages**: speech transcribed and translated together with on-screen
  text, in one Gemini call (`MAX_MEDIA_SECONDS`, `MAX_VIDEO_BYTES`, per-group `videos`/`voice` toggles).
* **One media call for all recipient languages** (was one call plus a text call per extra language).
* Media worker pre-warm at startup; per-stage media timings in `/status`.
* `show_sender` per-group option (default off); headers now read `🌐 Burmese → English`.
* Gemini failover model (`GEMINI_FALLBACK_MODEL`) on 503/overload; configurable thinking levels.
* Wrong-script validation for text and media translations (e.g. Hebrew letters for a Hindi reader are rejected).
* Live benchmarks: language matrix (12 × 6) and media (23 items incl. vector-text TGS, speech).

### Changed
* Default quality model → `gemini-3.5-flash-lite` (LOW thinking): larger free-tier models measured 10–40 s or 503.
* Media text gate is now a script-agnostic text **detector** with an aspect-ratio/recognizer filter (recall on
  non-Latin stickers 0% → 100%; smiley faces no longer count as text); gate runs at 640 px.
* Deadline split between primary and fallback provider (fixes silent drops when waiting for a rate-limit slot).
* Laughter tokens (kkkk, mdr, jajaja, www) mark messages informal → routed to the LLM.
* Prompt: native script always, localized laughter, slang never made cruder.
* Hinglish lexicon expanded; second detection rule for code-switched text.
* Time-format conversions (18:30 → 6:30 p.m.) no longer flagged as missing numbers.
* Messages are marked `done` after processing.


## 0.1.0 — 2026-11-21

Initial implementation.

* Private, recipient-only group translation via Telegram ephemeral messages (Bot API 10.2+); zero public output,
  enforced by `PublicOutputGuard`.
* Owner-managed approved recipients (default: owners only); per-user persisted languages (default English,
  one-time notice), pause/resume, per-group mute, optional consent-based private-chat backup.
* Private onboarding: `/start` welcome + language picker (popular grid, A–Z pages, type-to-search over 79
  languages), `/language`, `/settings`, `/groups`, `/pause`, `/resume`, `/help`, `/privacy`, `/forgetme`.
* Owner tools: setup verification report by DM, `/approve`/`/revoke` (hidden in-group reply or by ID),
  `/recipients`, `/chats` feature toggles, `/status`, glossary, failure digest.
* Local multi-signal language gate (script, wordfreq, lingua, Hinglish/Arabizi/slang lexicons).
* Providers: Gemini (generateContent, JSON schema, vision) + DeepL Free; routing, validation, one escalation,
  one fallback hop, circuit breakers, quota guards, request coalescing, in-memory cache.
* Media: images, static/animated/video stickers, GIFs — safe decoding, distinct-frame selection, RapidOCR gate,
  Gemini vision; sticker/GIF OCR cache.
* Inline translator with debounce, cache, approved-only access.
* Burst coalescing of rapid messages; edits update translations in place.
* SQLite storage with migrations and retention; polling/webhook; health endpoint; graceful shutdown; Docker.

### Fixed during review
* Greek question mark (`;`) made Greek questions look like code and get skipped.
* Code-fence-only messages were classified as emoji.
* Email domains were also extracted as URLs.
* Frame selection missed text changes on static backgrounds (dHash) → changed-pixel comparison.
* DM backup could reach a recipient removed from the group within the membership cache window.
* Photo OCR text was persisted (privacy) → only shared stickers/GIFs are cached.
* Chats first seen without a join event were auto-enabled.
* Context buffer keyed by non-forum thread ids.
* Translation ran (spending quota) when the bot wasn't admin.
* HTML-escaped text was sent to providers for plain messages.
