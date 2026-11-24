# Architecture

All-in-One Translator is a single-process, asyncio Telegram bot (Python 3.12+, aiogram 3.33 / Bot API 10.4) that acts as an
**invisible interpreter**: it reads group messages and shows each approved recipient a translation that **only
they can see**, using Telegram *ephemeral messages* (Bot API 10.2+). It never posts anything publicly.

## Data flow

```
Telegram ──► aiogram Dispatcher ──► handlers (return immediately)
                                      │
             ┌────────────────────────┴───────────────────────────┐
             ▼                                                    ▼
   group: Pipeline.on_message                          private: 1:1 translator (Pipeline.translate_private) + onboarding / settings
     1 chat config (cached 30s) · enabled? bot admin?   inline:  debounce → detect → translate
     2 eligibility (pure): bots, own inline results,
       commands, emoji/URL/code-only, toggles
     3 claim (chat_id,message_id) UNIQUE  → idempotent
     4 context buffer (memory, pseudonymized, TTL)
     5 burst coalescer (same sender, 700 ms)
             │
             ▼
     text lane (4 workers)        media lane (2 workers + process pool)
       recipients = (owners ∪ globally approved ∪ group-approved ∪ approveall participants) ∩ members − sender − paused − muted
       group by recipient language → per language:
         LanguageDetector gate  (TARGET → skip | OTHER | UNSURE)
         Orchestrator: cache → route → provider → validate → (1 escalation) → (1 fallback hop)
       Deliverer: one task per recipient → sendMessage(ephemeral_message_parameters=receiver)
         failure → record + owner digest → DM fallback only with consent + fresh membership check
             │
             ▼
   PublicOutputGuard (aiogram request middleware): any API call visible in a group raises before I/O
```

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| Delivery | Ephemeral messages only (`ephemeral_message_parameters.receiver_user_id`) | Requirement: zero group clutter. Admin bots may send them to any member at any time. |
| Enforcement | `PublicOutputGuard` request middleware + per-test assertions | Non-interference is structural, not a convention: public sends, replies, reactions, chat actions, pins, edits, deletes and forwards to a group are rejected before any network I/O. |
| Command menu | No group-scope commands; owner commands registered per owner per group with `is_ephemeral=True` | A group command menu would add a visible "/" button for every member. |
| Language detection | Local multi-signal gate (script + `wordfreq` + `lingua` + lexicons) | `lingua` alone misread "no manches" as French and "yaar kya scene hai" as Tagalog; word-frequency evidence fixes short chat. Uncertain cases are decided by the LLM in the same call that translates — no extra detection call. |
| Translation providers | Gemini (LLM, free tier) + DeepL API Free | User constraint: free providers only. DeepL's measured allowance is 1M chars/mo for clean prose; Gemini handles slang, Hinglish/romanized text, code-switching, context and vision. |
| Routing | clean→DeepL; informal/romanized/mixed/short/laughter→Gemini fast; ambiguous-in-context/OCR/failed-validation→Gemini quality; one fallback hop with a guaranteed share of the deadline; Gemini 503 → failover lite model | Saves the small Gemini free quota for the cases only an LLM handles well. Romanized text is never sent to DeepL (it can't translate it). |
| Models | Text: `gemini-3.1-flash-lite` for fast (MINIMAL thinking) and quality (LOW thinking), `gemini-3.5-flash-lite` failover. Vision/speech: `gemini-3.5-flash-lite`, failing over to the text model | Measured live on the free tier: 3.6/3.5-flash took 10–40 s, 3.7/3.8-flash returned 503, so only flash-lite is practical. In a 27-sample A/B of slang into ru/ar/hi, 3.5-flash-lite garbled 3–5 outputs (word salad, wrong script, cruder slang) at temperature 0 and 0.2; 3.1-flash-lite garbled none. 3.5-flash-lite stays on vision, where the media benchmark validated it. |
| Validation | Local checks: empty, identical, missing URLs/mentions/numbers (time-format aware), preamble, length blow-up, **wrong script** for non-Latin targets | No second LLM call per message; one escalation to the quality model on failure. Wrong-script output is never delivered. |
| Storage | SQLite (WAL) via aiosqlite, versioned migrations | Single instance; no Redis/Postgres needed. Message *text* is never persisted. |
| Queueing | In-process lanes with per-chat round-robin, bounded size, dedupe, timeouts | Text never waits behind GIFs; one busy chat can't starve others. |
| Media | Pillow/rlottie/ffmpeg in a pre-warmed process pool → script-agnostic text *detector* gate (aspect-ratio + recognizer filter for square boxes) → ONE Gemini call with ≤4 distinct text frames (+ Opus audio for video/voice ≤ 1 min) returning translations for all recipient languages | Textless media costs zero provider calls; works for every script (the bundled recognizer only reads Latin/CJK, so it can't be the gate); speech and on-screen text in one call; local OCR is the no-Gemini fallback. |
| Context | In-memory ring buffer per chat/topic (6 msgs, 15 min), pseudonymous speakers, sent only for short/ambiguous/reply messages | Accuracy where it matters, minimal data sent. |

## Modules

Personal reading preferences use migration 5; migration 4 remains unchanged for existing installations.
Additional languages suppress only confidently single-language group content, before text provider calls.
Caption and media reading preferences are applied separately so an understood caption cannot hide an
unfamiliar image's text. Recipients are resolved again after translation to honor revocation and settings changes.

Consecutive text deliveries may append to the reader's last ephemeral within 20 seconds, only when its last
original ID exactly matches the preceding message, sender, topic, target language and display preferences.
Per-reader locks serialize sends/edits. Bubble text stays in a bounded in-memory cache (2,000 entries), with
expired entries evicted on new sends. Edits invalidate the cached bubble; edits of shared translations use
the existing fresh-correction path rather than overwriting other originals. No message text enters SQLite.

| Module | Responsibility |
|---|---|
| `config.py` | Env/.env settings, validated at startup with actionable errors |
| `languages.py` | 79-language registry, name/alias/native lookup, never guesses |
| `textutil.py` | Content classification, entity extraction, Telegram-HTML sanitizer |
| `detect.py` | Three-way language gate relative to the target |
| `providers/` | `base` types & error taxonomy, `gemini` (generateContent, JSON schema, vision), `deepl` |
| `prompts.py` | Static system prompts (cache-friendly), style notes, JSON schemas |
| `orchestrator.py` | Cache, routing, retries, circuit breakers, validation, fallback, request coalescing |
| `resilience.py` | Sliding-window limiters, daily quotas, circuit breaker, jittered backoff |
| `eligibility.py` | Pure message classification |
| `context.py` | Bounded conversation memory |
| `recipients.py` | Approved-recipient resolution + membership cache |
| `delivery.py` | Ephemeral transport, formatting, per-recipient isolation, DM fallback, failure digest |
| `guard.py` | PublicOutputGuard |
| `workqueue.py` | Lanes and burst coalescer |
| `pipeline.py` | Group message / edit processing |
| `media/frames.py`, `media/processor.py` | Sandboxed decoding, frame selection, OCR, vision |
| `handlers.py`, `ui.py` | aiogram routers, keyboards, texts |
| `app.py`, `__main__.py` | Wiring, setup verification, maintenance, polling/webhook, health, shutdown |

## Cost vs quality

All providers run on free tiers, so "cost" is **quota**:

* **DeepL Free** — 1,000,000 characters/month (verified via `/v2/usage`). Context (`context` param) is not billed. A local budget
  (`DEEPL_MONTHLY_CHAR_BUDGET`, default 950k) stops before the hard limit.
* **Gemini free tier** — per-model RPM/RPD limits shown only in AI Studio. Local guards (`GEMINI_RPM`,
  `GEMINI_RPD`) keep the bot inside them; a 429 with a per-day quota violation disables that model until the
  UTC day rolls over; per-minute 429s honour `retryDelay`.
* **Per message**: one provider call per *distinct target language among recipients* (not per recipient).
  Bursts of rapid messages share one call. Identical text is served from an in-memory cache (6 h).
  Stickers/GIFs are cached by `file_unique_id` (30 days). Textless media never reaches a provider.
* **Quality levers you can turn**: switch `GEMINI_FAST_MODEL` to the quality model (better slang, ~half the
  free quota), set `BURST_WINDOW_MS=0` (no coalescing; more calls), or remove DeepL to send everything through
  Gemini (more natural for chat, far less quota).

## Telegram facts this design depends on

Verified against Bot API docs / aiogram 3.33 (see `COMPETITIVE_ANALYSIS.md` for sources). Items marked ⚠ must be
confirmed live (see `PLAN.md` → live verification).

* Ephemeral messages (10.2+): visible only to `receiver_user_id`; admin bots can send to any non-bot member at
  any time; `message_id` is 0 and `ephemeral_message_id` identifies it; editable via `editEphemeralMessageText`.
  ⚠ Delivery "is not guaranteed, especially if the user is offline", and they "may disappear after some time or
  if the app is restarted". The API can report success for a message the user never sees.
* Admin bots receive all group messages regardless of privacy mode; no bot receives other bots' messages.
* No deletion updates exist for normal groups → deleted originals cannot be mirrored (documented limitation).
* `getFile` downloads ≤ 20 MB (we cap at 8 MB). Inline queries ≤ 256 chars; results' message text ≤ 4096.
* Adding the bot creates a public "X added the bot" service message and the bot appears in the member list —
  no bot can avoid this.

Personal controls use explicit pause/resume callback actions, so repeated clicks on old keyboards do not
reverse the intended setting. Private help and settings accept group-approved readers, including current
seen members in approve-all groups, while private translation and inline authorization stay global.

`/approveall` requires a fresh Telegram group-owner (`creator`) check without a bot-owner override.
Other administrators can still approve individuals and disable approve-all with `/revokeall`.
Group command menus are registered only per member, with ephemeral commands; broad group/admin scopes
are removed during registration. The output guard rejects broad/default command registration and
non-ephemeral per-member commands. Approval invalidates membership rather than trusting an old reply.
