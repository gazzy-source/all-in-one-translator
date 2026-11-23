# Guidance for coding agents

## Read first

Inspect the current code and configuration before relying on documentation. Then read `README.md`,
`ARCHITECTURE.md`, `SECURITY.md` and `PLAN.md` (status and live-verification log). If they disagree, current
code/configuration wins and the docs should be corrected as part of the work.

## Non-negotiable product rules

- **Zero public group output.** The bot only sends ephemeral messages to specific receivers (Bot API 10.2+).
  `PublicOutputGuard` (`src/translator_bot/guard.py`) must keep rejecting every group-visible method. Never add a
  group-scope command menu, reactions, pins, edits or deletes of members' messages.
- **Recipients are (bot owners ∪ users approved by a bot owner ∪ users approved by this group's owner/admins
  ∪, when the group's admins chose `/approveall`, the people seen in that group) ∩ current members**, re-checked
  per message, fail-closed. Group-admin approval is valid only in that group.
- **Message text is never written to disk or logged.** Only IDs/hashes, and OCR text of shared stickers/GIFs.
- **Provider output is untrusted.** Plain-text sources are HTML-escaped; HTML sources keep only links present in
  the original (`restrict_links`). Validation (`orchestrator.validate`) rejects wrong-script and garbled output.

## Current philosophy

Single-instance bot: asyncio, in-process queues, SQLite (WAL). Do not introduce Redis, Celery, Kafka, multiple
workers or paid providers unless a measured requirement justifies it. Free tiers (Gemini, DeepL Free) shape the
design: routing, coalescing, caching and quota guards exist to stay inside them.

## Engineering rules

Before a quality, routing or model change, answer: what measured problem exists, what evidence proves it, what is
the smallest reversible fix, what test proves it, and which benchmark verifies it. Benchmarks live in
`benchmarks/` (`bench_translation.py`, `bench_language_matrix.py`, `bench_media.py`, `bench_detection.py`); record
live results in `PLAN.md`. Prefer focused regression tests and no unrelated refactors in bug-fix commits.

## Checks before committing

```bash
pytest && ruff check src tests && mypy && bandit -q -c pyproject.toml -r src
```
