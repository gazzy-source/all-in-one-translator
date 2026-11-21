# Contributing to All-in-One Translator Bot

Thanks for helping make Telegram groups readable in every language — without the bot ever getting in the way.

## Ways to contribute

- Bug reports with repro steps (source language, target language, message type — text, sticker, GIF, voice…)
- Language-detection misses: short phrases or slang the local gate gets wrong (add them to
  `benchmarks/detection_corpus.json`)
- Translation-quality cases for `benchmarks/translation_corpus.json`
- Documentation fixes
- Tests for pure helpers (`src/translator_bot/textutil.py`, `detect.py`)

## Development setup

```bash
git clone https://github.com/gazzy-source/all-in-one-translator.git
cd all-in-one-translator
python -m venv .venv
source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[media,dev]"    # media extras need ffmpeg on PATH for the media tests
cp .env.example .env             # BOT_TOKEN, OWNER_IDS, GEMINI_API_KEY, DEEPL_API_KEY — live runs only
python -m translator_bot
```

## Checks

```bash
pytest                      # all providers and Telegram are mocked; no secrets needed
ruff check src tests
mypy                        # strict
bandit -c pyproject.toml -r src
```

## Code guidelines

1. **No secrets** in code, commits, logs or screenshots (bot token, API keys, real user IDs).
2. **Never post publicly in a group.** All group output goes through ephemeral messages; `PublicOutputGuard` must
   keep rejecting everything else. New Telegram calls need a guard test.
3. **Never persist or log message text.** IDs and hashes only (see `SECURITY.md`).
4. Quality or routing changes need evidence: run the relevant benchmark in `benchmarks/` and include the before/after
   numbers in the PR.
5. Prefer small, focused PRs; new dependencies need a short justification.

## License

By contributing, you agree your contributions are licensed under the MIT License (same as this repository).
