# Plan & status

Legend: ✅ done & mock-tested · 🟢 live-verified · ⏳ pending live verification (needs credentials/accounts) ·
❌ not done.

## Phases

### 2026-11-23 — completion of pending reader preferences and consecutive bubbles

* Pre-deployment inspection: `/opt/all-in-one-translator`, image/package **1.3.0**, commit `8e6d5bb`, schema **3**;
  container healthy, zero restarts. This inspection did not deploy changes or send Telegram messages.
* Measured defects in the unfinished changes: upgrading a committed schema-4 database left the two new
  user columns missing (`IndexError` on user lookup); the existing revocation test expected two sends
  while the new behavior correctly used one send plus one edit. Review also found that one source label
  could hide unfamiliar content in mixed messages/captioned media, and cached bubbles lacked ordering,
  language and display-preference checks.
* Fixes: separate migration **5**, conservative additional-language filtering before text provider calls,
  independent caption/media filtering, complete paginated language picker, matching original-text quotes,
  exact predecessor-ID checks, per-reader delivery locks, bounded bubble memory and edit invalidation.
  Recipients are resolved again after translation to honor revocation, pause and changed preferences.
* Regression evidence: version-3/4 upgrades preserve approvals, preferences and purchased credit;
  `/forgetme` clears new preferences; understood source text for all readers makes **zero provider calls**;
  consecutive text uses **one send plus an edit**; expired/oversized/incompatible bubbles start fresh;
  membership failures trigger no resend or DM; corrections preserve the other shared translations.
* Offline detection benchmark: **107 cases, 0% false-translate, 0% missed, 98.25% source-language accuracy,
  6.54% deferred**. Measured 118.75 ms/message while test processes ran concurrently; this timing is not
  comparable to the earlier isolated latency runs. No detector thresholds, model defaults or routing changed.
* Final pre-commit validation (Windows, Python 3.13): **472 passed in 312.11 seconds** in one complete
  `pytest -q --tb=short` run, including native media tests. `ruff check src tests`, strict `mypy`,
  `bandit -q -c pyproject.toml -r src`, the corrected provider-smoke script's lint, and `git diff --check`
  pass. Bandit needed access to its plugin cache outside the sandbox; it reported no security findings.
  The Telegram test double now applies its failure flag to edits as well as sends, covering membership
  failures on the append path. GitHub CI for `575db6a` also passed **472 tests on both Python 3.12 and 3.13**,
  the production Docker build/native-media smoke test, and a dependency audit with **no known vulnerabilities**
  ([run 45162424596](https://github.com/gazzy-source/all-in-one-translator/actions/runs/45162424596)).
* Production log review: no WARNING/ERROR/CRITICAL events in the checked 15-minute window. These are
  bounded health/provider checks, not a guarantee of latency or every real-device behavior.
* **Deployed 2026-11-23:** v1.4.0, application commit `575db6a`, image
  `all-in-one-translator:1.4.0-575db6a`, schema **5**. The image reuses the existing v1.3.0 runtime to avoid
  reinstalling unchanged dependencies on the 952 MB VPS; all 33 application Python/data files and runtime
  dependency versions were compared successfully before cutover. Low-memory settings were preserved.
* Recovery: pre-cutover backup retained; restoring the schema-3 backup and migrating it to 5 preserved all
  existing records and passed integrity checks. The first schema-5 backup was also restored and verified.
  Daily local backup runs at **03:15 UTC**, keeps **14**, and uses private file permissions (0600).
  Rollback image/configuration and a syntax-checked runtime rollback script are retained under
  `/home/ubuntu/translator-deployments/20261123-575db6a-112011/`. Rollback preserves the live database.
* Post-deployment health/provider checks passed (L22). Visual acceptance of the new bubble UI and
  offline/reconnect behavior still need real-device verification; no Telegram test messages were sent.
  Backups are local to the VPS; off-host backup is not configured by this deployment.

| Phase | Status |
|---|---|
| 1 Research & audit (repo was empty; Telegram Bot API 10.4; competitors; Gemini/DeepL docs; OCR options) | ✅ |
| 2 Architecture (ARCHITECTURE.md) | ✅ |
| 3 Core private group translator | ✅ mock · 🟢 live (L3–L5, L9, live group test 2026-11-22) |
| 4 Inline translator | ✅ mock · ⏳ live (not exercised in the live group yet) |
| 5 Media (captions, images, stickers, TGS, video stickers, GIFs, voice, video) | ✅ mock · 🟢 live (L11, L12, L14, media matrix, live group stickers/GIFs) |
| 6 Intelligence (context, glossary, mixed/romanized, routing, validation) | ✅ mock · 🟢 live quality (L8, L13, L15) |
| 7 Reliability (queues, breakers, fallbacks, shutdown, metrics, health) | ✅ mock |
| 8 Testing (unit, e2e mocked, detection benchmark, security tools) | ✅ · 🟢 live benchmarks & 3-account test (L8, L9) |
| 9 Optimization (measure latency/quota live) | 🟢 latency measured (p50 0.4–0.7 s text, ~2–3 s media); ⏳ load test |
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
| L16 | VPS (Oracle E2.1.Micro: 1/8 OCPU, 952 MB RAM, shared with other services): default build swapped to a halt — wordfreq tables are ~1 GB (lingua ~0). `DETECTOR_WORDLIST=small`: RSS 975→256 MB, gate 37→4.6 ms/msg, `bench_detection` identical (FP 0, miss 0, lang 98.2%); 34/3,216 gate decisions differ over all corpus texts × 16 targets (mostly source label at OTHER; one code-switch es line now translated). Garble check kept on full lists. Live on VPS: text 20 cases p50 1.2 s, 0 errors, source-lang 100%; CPU steal 50–75% under load | 🟢 (small-RAM mode) |
| L17 | Bench 2026-11-22 (47-case corpus + 48 held-out): 0 silent misses / false translations; all low-chrF outputs correct in meaning on reading. Found: 24-h time false positive (15 s waits), Russian labelled Belarusian, DeepL fallback garbling accentless Vietnamese, DeepL em dashes, context leak. After fixes (held-out, 4 s pace): source-lang 97.6→100%, p50 1.21→1.04 s, it-1 14.2→0.4 s. Latency spikes left: Gemini free-tier timeouts (failover to 3.5-flash-lite) | 🟢 |
| L18 | Gemini hedging (fallback model asked after 3 s; RPM-slot switch; timeout failover), held-out 48 at 4 s pace: p50 1.11→0.85 s, p90 11.0→4.6 s, >5 s 8→2, max 12.9→9.9 s; chrF 64.8, silent 0, false 0, source-lang 100%. Hedged winners arrive ~4–4.6 s → default lowered to 2.5 s | 🟢 |
| L19 | Burmese group check (live providers): Unicode my→en 0.3 s ✓, Zawgyi my→en ✓ (DeepL normalizes it), ၇→7 ✓, en→my native script ✓. Burglish was read as Tagalog → DeepL nonsense: fixed with a romanized-Burmese lexicon (Gemini only). Found control-character junk passing validation (fixed). Burglish meaning stays ambiguous for some lines ("sar", "pyaw"); fast vs quality model no clear difference on 6 lines | 🟢 (Burglish ⚠ ambiguous) |
| L20 | Mixed languages, live: 7 code-switched texts × readers (en/my/es) and 2 two-script stickers. Found and fixed: foreign phrase inside target-language text dropped; English sentence lost for Burmese readers; mixed stickers dropped/half-translated a line via vision (now text path). After: all correct (e.g. "WHEN THE TEACHER SAYS / Exam tomorrow"); trivial English ("ok bro") still skipped for Burmese readers; bench_detection unchanged | 🟢 |
| L10 | Offline recipient: are ephemeral translations shown after reconnecting? after app restart? | ⏳ product decision depends on result |
| L21 | 2026-11-23 production pre-commit check (still **v1.3.0**, schema 3): container healthy, zero restarts; `/healthz` ok, 4/4 text workers and 1/1 media worker alive, both provider breakers closed, zero blocked public-output attempts. SQLite `quick_check` ok; Telegram authentication succeeds; bot is administrator in the enabled group. Synthetic provider probes: Gemini fast and quality both translated contextual “ni modo” as “oh well” using the 3.5-flash-lite fallback (11,849 ms / 5,618 ms); vision read “NO WAY BRO” and translated to Spanish (6,584 ms); DeepL preserved URL/mention and converted 18:30 correctly (533 ms). Configured Gemini models available; DeepL usage 22,674/1,000,000 chars before the 46-char probe. Corrected the smoke script's old single-target vision shape and removed its test-fixture dependency. No Telegram messages sent; no production deployment. Gemini latency remains variable. | 🟢 service/provider checks; new UI still pending live acceptance |

### L22 — Production deployment, 2026-11-23

Container v1.4.0 started at **11:27:28 UTC**, healthy with zero restarts;
all four text workers and the media worker alive, empty queues, both provider breakers closed, zero blocked
public-output attempts. Live schema **5**, `quick_check` OK, new preference fields readable, Telegram
authentication and administrator status in the enabled group verified. No warning/error/critical log events
were observed after startup. Synthetic provider probes on the new container: Gemini fast **2,445 ms** and
quality **1,616 ms** (both 3.1-flash-lite; contextual “ni modo” → “oh well”); vision **2,668 ms** (read “NO WAY
BRO” and translated to Spanish); DeepL **398 ms** (URL/mention/time preserved). These are individual samples,
not latency guarantees. No real group message was observed during this check; client-side rendering remains
pending. The pre-cutover backup is `data/predeploy-575db6a/translator-20261123-112716.sqlite3`; the first verified
schema-5 backup is `data/backups/translator-20261123-113123.sqlite3`.

### L23 — Multi-account group simulation, 2026-11-23

Added `tests/test_group_matrix.py`: 13 synthetic users, eight languages (en/es/hi/my/ar/ru/ja/zh),
two groups and separate topics. The 13 tests exercise recipient/language isolation, shared translations,
group-local administrator approvals, access/preference changes during translation, expired bubbles,
topic isolation and 24 concurrent multilingual messages (189 expected recipient records).
Existing regressions cover mixed-language text, edits, media, approval-all, provider failures and output guards.

`python -m benchmarks.bench_group_simulation --live` uses real providers with the real handlers/queues,
but **simulates Telegram and all accounts** in an isolated temporary database. It cannot verify actual
Telegram rate limits, client rendering or offline delivery. Reports contain validation results and hashes,
not message/translation text. Transport limits are raised only in this harness; provider limits remain active.

Initial run (`group-simulation-20261123-222400.json`): 63/63 expected deliveries, zero unauthorized/public
output, five entity-validation flags. Synthetic inspection identified a false positive: DeepL renders
`10:00` as Burmese `၁၀ နာရီ`. Added only recognition of Burmese clock units/native minute digits, with six
regression cases including changed hours/minutes and ordinary numbers. Re-run
(`group-simulation-20261123-222907.json`): **63/63 deliveries, 72/72 recipient checks, zero validation flags,
zero unauthorized/public output**, across 56 language pairs. Successful provider calls: 50 DeepL, six Gemini.
Gemini still reported transient/time-out/rate-limit errors (4/6/6); eight fallback hops recovered delivery.
This validates routing, script/entity checks and isolation, not exhaustive semantic translation quality.
Final local checks: **491 pytest cases passed**; ruff (`src`, `tests`, new benchmark), strict mypy and bandit
passed. No routing/model changes or new runtime dependencies were introduced.

Deployed application commit `62cac9a` as `all-in-one-translator:1.4.0-62cac9a`; installed `textutil.py` hash
matches the committed source. Container healthy, zero restarts; 4/4 text and 1/1 media workers alive,
empty queues, provider circuits closed, guard-blocked count zero. A synthetic en-to-my provider probe
inside the deployed image passed validation (DeepL 1,001 ms). Startup took several minutes on the small VPS;
ONNX warned that its telemetry identifier could not be persisted on the read-only filesystem and used memory.
Pre-update backup: `data/backups/translator-20261123-170610.sqlite3`. Previous image and private environment
snapshot retained under `/home/ubuntu/translator-deployments/20261123-170456-62cac9a`.
CI for `62cac9a` passed on Python 3.12 and 3.13, including Docker/native-media smoke and vulnerability checks:
https://github.com/gazzy-source/all-in-one-translator/actions/runs/45223611798.

### Personal controls and navigable help — 2026-11-24 (local)

Welcome/help now expose reading language, personal pause/resume and per-group mute controls, with separate
reading, writing, setup and troubleshooting guides and return navigation. Explicit pause/resume callbacks
are idempotent; choosing a language preserves pause. Group-local approval and current approve-all participants
can access personal controls under restricted private access without receiving global/private translation
privileges. Typed language selection also works for those readers; other text still obeys private access.
No model, routing, schema, provider, or public-output changes. Slash-command auto-translation remains skipped.
Full regression suite: **486 passed, 8 skipped** (196.35 s). After final navigation/typed-language edits,
focused tests: **64 passed** (`test_e2e_group.py`, `test_reading_preferences.py`); ruff, strict mypy,
bandit and diff whitespace checks passed. Telegram/provider calls were mocked; no deployment or live client
verification was performed for this change.

### Owner-only approve-all and bot review — 2026-11-24 (local)

`/approveall` now requires a fresh Telegram `creator` check, with no bot-owner override. Group admins
retain individual approval and `/revokeall`. Help, setup guidance and command menus reflect this policy.
Review found two additional defects: approving a historical message could overwrite a departed member's
cached status with true, and group-wide command registration bypassed the output guard. Approval now
invalidates membership; command registration is per person and ephemeral only, with broad scopes removed
at registration and rejected by the guard. No message content is stored, no provider/model/routing change.

Read-only production inspection: image `1.4.0-62cac9a`, healthy, zero restarts, all four text workers and one
media worker alive, empty queues, both circuits closed, zero blocked public-output attempts. Last-24-hour
logs contained six membership-check warnings, six provider warnings (transient/blocked/timeout), one vision
fallback, and one video-frame extraction timeout. These warnings do not establish which individual
messages were ultimately missed. No ERROR/CRITICAL records were returned in that window.
Validation: full suite **494 passed, 8 skipped** (263.50 s); after the final command-menu hardening,
**79 focused integration/guard tests passed**. Ruff, strict mypy, bandit and diff whitespace checks passed.
Changes remain local; no deployment or real-user message was sent during this review.

## Measured so far (offline)

* Language gate, 103-case corpus authored for this project (not held-out): false-translate 0%, missed 0%,
  source-language accuracy 98% (ms↔id confusion), deferred-to-LLM 6.8%, ~21–34 ms/message on this laptop.
* Earlier recorded CI suite: 383 passed (Ubuntu, Python 3.12 + 3.13). Current local validation is recorded above
  (all providers/Telegram mocked; media tests use real ffmpeg/rlottie/RapidOCR on synthetic images).
* ruff, mypy --strict, bandit: clean. pip-audit: no known vulnerabilities.

## Private navigation and recent translations

The home screen combines the previous welcome and language picker into one message. Recent deliveries
are kept in a bounded in-memory reader for up to 15 minutes. Opening a group or topic page checks current
access again. A reply with /tr can reuse an exact previous delivery. Visible /play song arguments
now enter the ordinary translation path. Queue rejection is recorded separately from successful enqueueing.

Pre-deploy VPS check on November 24: healthy, zero container restarts, all four text workers and one media
worker alive, queues empty. A direct Gemini probe returned HTTP 503. Synthetic probes through the configured
endpoint translated a meeting-time question Burmese → English in 16.6 s and English → Burmese in 8.0 s.
These probes exercised the provider without sending messages to Telegram users.

## November 25 — concurrent group messages and pending edits

Two dispatcher regressions reproduced on the prior release: a delayed SQLite claim reversed a
two-line burst, and reverting an edit before the burst flushed left its intermediate text in the translation.
A 40-message, four-topic simulation also reproduced reordering in asynchronous router filters before the
pipeline. Group messages and edits now enter those filters in arrival order within each topic. Provider
and delivery jobs still run concurrently. Pending edits update the stored content hash before returning.
The regression scenarios use simulated Telegram accounts and providers, including a one-off translation
of an older reply when Telegram omits its nested parent.

The read-only production audit found 2,238 recorded successful ephemeral deliveries and 24 source rows
marked processing over the preceding 24 hours, with empty queues at inspection. A confirmed path now
records no_recipients when a text or media job has no eligible readers. Historical rows do not establish
the cause of each individual case.

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
