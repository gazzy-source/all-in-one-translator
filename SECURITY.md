# Security

## Reporting a vulnerability

**Please don't open a public issue for security problems.** Report privately through a
[GitHub Security Advisory](https://github.com/gazzy-source/all-in-one-translator/security/advisories/new) on this
repository. Include the affected version/commit, steps to reproduce and the impact; redact tokens, API keys and
other people's messages. Supported version: `main` (latest); older tags best-effort.

# Security review

Original audit scope: `src/translator_bot` at v0.2.1; subsequent changes are noted below. Tools: bandit 1.9.4 (clean after reviewed suppressions), pip-audit 2.10.1
(no known vulnerabilities in dependencies), ruff security rules (S), mypy --strict, and a manual review.

| Area | Risk | Mitigation | Verified by |
|---|---|---|---|
| Public output / group interference | Bug or future change posts in a group | `PublicOutputGuard` request middleware rejects every group-visible method (send/forward/copy/react/pin/edit/delete/chat action) unless it is an ephemeral send/edit to a specific receiver; no group-scope command menu | `tests/test_guard.py` (incl. live request chain), e2e `public_group_output() == []` |
| Unauthorized recipients | Non-approved user receives translations | Recipients = (owners ∪ globally approved ∪ group-approved ∪ participants under that group's `/approveall`) ∩ members, re-checked per message and after translation; membership verified via `getChatMember` (cached ≤10 min, fail-closed) and invalidated by `chat_member` updates; DM fallback re-checks membership uncached and never fires on `not_member` | `test_e2e_group`, `test_regressions` |
| Private-chat quota abuse | Strangers DM the bot to use up the free-tier quota groups depend on | `PRIVATE_ACCESS` (everyone \| approved); per-user `PRIVATE_PER_MIN`; per-user and total per-day caps for users who aren't approved (`PRIVATE_DAILY_LIMIT`, `PRIVATE_GLOBAL_DAILY_LIMIT`; in memory, reset on restart); private jobs share the bounded lanes. Replies go only to the sender's own private chat; group delivery still requires approval | `test_e2e_group::test_private_daily_limit_for_unapproved`, `test_private_start_access_denied_when_private_access_approved` |
| Payment abuse | Forged/replayed payments, price tampering, double credit | Stars only (XTR); invoice payload binds product, buyer and price; pre-checkout re-validates them against the current plan; `successful_payment` re-checked (currency, buyer, amount) and credited once per `telegram_payment_charge_id` (unique key); mismatches are not credited and reported to owners; refunds revert the grant once; purchases survive `/forgetme`; in-flight translations are reserved so bursts can't overshoot the allowance | `test_billing` |
| Unauthorized approval | Members approve themselves or others | `/approveall` requires a fresh Telegram `creator` check, with no bot-owner override. `/approve`·`/revoke`·`/revokeall` require a bot owner or that group's creator/administrator (live `getChatMember`, 60 s cache, errors deny); group-admin approval is stored per group and never grants other groups; all are hidden commands with ephemeral answers | `test_e2e_group::test_group_admin_approves_members_for_their_group`, `test_group_admin_approveall_translates_for_everyone_there` |
| Unauthorized configuration | Members change settings | Owner-only commands check `OWNER_IDS`; group owner commands silently ignored for others; callbacks re-verify approval/ownership; settings keyboards only work in private chat | e2e tests |
| Cross-user / cross-group leakage | One user's prefs/translations visible to another | Ephemeral messages are receiver-scoped; per-user rows keyed by Telegram ID; translation cache shares only context-free, content-addressed entries (same input → same output); context/glossary-dependent entries are chat-scoped | `test_orchestrator::test_cache_and_context_scoping` |
| Token / key exposure | Secrets in logs, URLs, errors | `SecretStr` settings; config errors list field names only; Gemini key sent in header (`x-goog-api-key`), never URL; provider errors keep only status/reason; httpx/httpcore loggers at WARNING; `.env` git-ignored | `test_providers` (key not in URL / error) |
| Logging private content | Message text in logs | No log call includes message text; metrics are counters/latencies only | code review |
| Prompt / OCR injection | Message tells the LLM to do something else | Message is JSON *data* in the user turn; system prompt says it is untrusted and must be translated, never followed; structured JSON output; output sanitized to a Telegram-HTML whitelist; translations can't trigger bot actions | `test_gemini_prompt_injection_stays_data`; live benchmark case `es-inject-1` |
| HTML injection / link spoofing | Provider output breaks Telegram HTML or adds a disguised link (`<a href=evil>your bank</a>`) via prompt injection or OCR'd text | Plain-text sources and all image/sticker/voice text: model output is HTML-escaped (no markup possible). HTML sources: `restrict_links` keeps only hrefs present in the original, then the whitelist sanitizer (`b,i,u,s,code,pre,a[http/https/tg/mailto],tg-spoiler,blockquote`) drops unknown tags; parse failure → plain-text resend | `test_textutil::test_sanitize`, `test_restrict_links_drops_links_not_in_source`, `test_e2e_group::test_model_cannot_inject_links_into_plain_translation` |
| SQL injection | Dynamic SQL | Values always bound; dynamic column names only from fixed allow-lists (unknown fields raise) | `test_upsert_rejects_unknown_fields` |
| Malicious media | Decompression bombs, gzip bombs, huge videos, parser exploits | Size cap before download (8 MB; Telegram cap 20 MB); Pillow `MAX_IMAGE_PIXELS` + dimension checks + `verify()`; TGS read with byte cap and Lottie bounds (≤1024 px, ≤1800 frames), and rejected if it has image layers/assets or font files (rlottie would open local paths); ffmpeg: argv list (no shell), fixed temp names, `-protocol_whitelist file`, demuxer forced and whitelisted per expected container (`-f`/`-format_whitelist`: no HLS/concat/image2 probing → no local-file reads), MP4 `enable_drefs 0`, `-t`/frame caps, 1 thread, timeout; native decoders run in a separate process pool, recycled on timeout | `test_media` (bombs, garbage, limits, disguised playlist, TGS image assets) |
| Path traversal / SSRF | Attacker-controlled paths/URLs | Files only fetched via Telegram `getFile` paths; temp files have fixed names inside `TemporaryDirectory`; no user-supplied URL is ever fetched | code review |
| Temp files | Media left on disk | `TemporaryDirectory` context managers; images processed in memory | code review |
| Resource exhaustion / DoS | Floods, huge texts, inline spam | Bounded lanes (global + per chat), dedupe, job timeouts, burst coalescing, `MAX_TEXT_CHARS`, per-user inline limiter + debounce, provider RPM/RPD guards, circuit breakers | `test_storage_queue` |
| Webhook spoofing | Fake updates | Secret token required (≥16 chars) and verified by aiogram | config validation |
| Deserialization | Unsafe loads | Only `json`; no pickle of external data (process-pool arguments are our own bytes) | code review |
| Edit-path gate parity | Edits bypass checks applied to new messages | Edits pass the same gates: chat enabled/active/admin, disabled topics, `MAX_TEXT_CHARS` truncation; only messages *sent* within `EDIT_WINDOW_S` are retranslated | `test_e2e_group::test_edits_respect_disabled_topics_and_send_date` |
| Permissions | Over-privileged bot | Works as admin with **all rights off**; never needs delete/pin/edit rights | setup report |

## Remaining concerns

Current reading-preference changes add migration 5 without rewriting migration 4. Additional-language skips
are conservative for mixed/uncertain text and apply independently to captions and extracted media text.
Recipient resolution is repeated after provider calls, before delivery, so a mid-request revocation or pause
blocks delivery. Membership still uses the existing bounded cache and member-update invalidation.
Consecutive-bubble text is kept only in bounded process memory. Per-reader locks serialize ephemeral updates;
membership/forbidden edit errors do not trigger resends or private-chat fallback. Regression coverage:
`test_reading_preferences.py` and `test_e2e_group.py`.

* Free-tier providers may use submitted text to improve their services. For sensitive
  groups use paid tiers with no-training terms.
* Ephemeral messages are processed by Telegram servers like any message; this is not end-to-end encryption.
* Process-pool hard-kill uses a private CPython attribute (best effort); on hang the pool is replaced regardless.
* The live Telegram behaviour of ephemeral messages (offline delivery, persistence) is documented by Telegram as
  best-effort and must be observed in the three-account test.

Personal help/settings access is separate from private translation access: group-local approval and
approve-all participation allow readers to manage their own preferences, not to gain global approval.
Help callbacks are private-chat only; state changes use the callback sender ID. Regression tests cover
recipient isolation, repeat pause/resume clicks, and restricted private-chat access after using controls.

Approval hardening: only Telegram's current group owner can enable `/approveall` (uncached, errors deny).
Approving an old message never establishes current membership: the recipient cache is invalidated and
membership is fetched again before delivery. Group-wide command registrations are blocked by the guard;
per-member registrations require ephemeral commands. Tests cover bot-owner/admin denial, ownership
transfer, lookup failure, departed recipients, and command-menu scope enforcement.
