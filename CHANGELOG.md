# Changelog

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
