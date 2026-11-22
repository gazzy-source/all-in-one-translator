# Deploying to production

The bot is a single long-running process: Telegram updates in, private (ephemeral) translations out, SQLite on
disk. It needs no public URL in the default **polling** mode, so any always-on Linux machine works: a small VPS,
a home server, or a Raspberry Pi 4+.

**Memory:** about 1.2 GB with the default settings (most of it is the language gate's word-frequency tables), so
plan for a 2 GB machine. On a **1 GB** machine set the low-memory mode in `.env`:

```bash
DETECTOR_WORDLIST=small   # ~0.1 GB of word lists instead of ~1 GB; same benchmark scores (PLAN.md L16)
MEDIA_WORKERS=1           # one OCR/render worker process (~150 MB each)
OCR_GATE_FRAMES=3         # slow/burstable CPUs: check fewer GIF frames locally
MEDIA_TIMEOUT_S=40        # ... and allow each media step a bit longer
```

The bot then idles at about 250 MB. A 1/8-core burstable VM (e.g. Oracle's free E2.1.Micro) translates text in
~1 s but is slow on stickers/GIFs while its CPU is throttled.

## 1. One-time Telegram setup

1. **@BotFather**: `/newbot` → copy the token. `/setinline` → enable inline mode (optional). Privacy mode can
   stay **on**: admin bots receive group messages anyway.
2. Your numeric Telegram ID (e.g. from @userinfobot) → `OWNER_IDS`.
3. Free API keys: [Gemini](https://aistudio.google.com/apikey) and/or [DeepL API Free](https://www.deepl.com/pro-api)
   (both recommended: DeepL for clean prose, Gemini for slang, romanized text, stickers, GIFs and voice).

## 2. Run with Docker (recommended)

```bash
git clone https://github.com/gazzy-source/all-in-one-translator.git
cd all-in-one-translator
cp .env.example .env && nano .env           # BOT_TOKEN, OWNER_IDS, GEMINI_API_KEY, DEEPL_API_KEY
mkdir -p data && sudo chown 10001:10001 data   # the container runs as uid 10001 (non-root)
docker compose up -d --build
```

Or use the published image instead of building (`v*` tags are published to GitHub Container Registry):

```bash
TRANSLATOR_IMAGE=ghcr.io/gazzy-source/all-in-one-translator:latest docker compose up -d
```

What the compose file sets up: automatic restart, a health check (`/healthz` reports a dead queue worker or an
unresponsive database), `init` to reap ffmpeg child processes, read-only root filesystem, all capabilities
dropped, 2 GB memory cap, JSON logs rotated at 5 × 10 MB. Self-healing: if a health problem persists for two
checks (~2 min), the bot shuts down cleanly with a non-zero exit and Docker (or systemd) restarts it — Docker's
"unhealthy" status alone never restarts a container.

**Verify:**

```bash
docker compose ps                      # STATUS should become "healthy" within ~2 minutes
docker compose logs -f --tail 50       # "Run polling for bot @..." then one line per update
```

Then send `/status` to the bot in a private chat (owners only): uptime, deliveries, provider errors, media skips.

## 3. Run without Docker (systemd)

```bash
sudo apt install python3.12-venv ffmpeg
sudo useradd --system --create-home --home-dir /opt/translator translator
sudo -u translator git clone https://github.com/gazzy-source/all-in-one-translator.git /opt/translator/app
cd /opt/translator/app
sudo -u translator python3 -m venv .venv
sudo -u translator .venv/bin/pip install ".[media]"
sudo -u translator cp .env.example .env    # then edit it
```

`/etc/systemd/system/translator.service`:

```ini
[Unit]
Description=All-in-One Translator (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
User=translator
WorkingDirectory=/opt/translator/app
ExecStart=/opt/translator/app/.venv/bin/python -m translator_bot
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
Environment=LOG_JSON=true
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/translator/app/data
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now translator
journalctl -u translator -f
```

## 4. Group setup

1. Add the bot to the group and promote it to **admin with every right switched off** — admin status is what
   lets it send private in-group messages; no rights are used.
2. You receive a setup report by private message. Groups added by non-owners start disabled.
3. Approve readers: reply `/approve` to one of their messages (only you see the command and the answer), or
   `/approve <user id>` in a private chat with the bot.
4. Each reader opens the bot, sends `/start` and picks a language.

**Private chat:** anyone can also send the bot text, stickers, GIFs, photos, videos or voice notes privately and get
a translation back (`PRIVATE_ACCESS=everyone`, the default). Users who aren't approved are capped per day
(`PRIVATE_DAILY_LIMIT`, `PRIVATE_GLOBAL_DAILY_LIMIT`), so strangers can't use up the Gemini quota your groups need.
Set `PRIVATE_ACCESS=approved` to keep the bot entirely private.

## 5. Backups

The database holds settings, approvals, language choices and IDs/hashes — never message text. Back it up with
SQLite's online backup (safe while the bot runs):

```bash
# Docker
docker compose exec translator python -m translator_bot.backup          # -> data/backups/
# systemd / bare metal
.venv/bin/python -m translator_bot.backup /path/to/backups
```

Daily cron (keeps the newest 14; change with `BACKUP_KEEP`):

```cron
15 3 * * * cd /path/to/all-in-one-translator && docker compose exec -T translator python -m translator_bot.backup
```

Restore: stop the bot, copy a backup over `data/translator.sqlite3` (delete `-wal`/`-shm` files), start it.

## 6. Updating

```bash
git pull && docker compose up -d --build          # or: docker compose pull && docker compose up -d
```

Schema migrations run automatically at startup. Translations queued at shutdown are drained for up to 30 s.

## 7. Webhook mode (optional)

Polling is simplest and needs no inbound port. For webhooks, put the bot behind an HTTPS reverse proxy:
`RUN_MODE=webhook`, `WEBHOOK_URL=https://your.domain/webhook`, `WEBHOOK_SECRET=<16+ random chars>`, expose
`LISTEN_PORT` to the proxy only (see the commented `ports:` in `docker-compose.yml`). `/healthz` is served on
the same port.

## 8. Limits and costs (free tiers)

| | Free allowance | What the bot does |
|---|---|---|
| Gemini (flash-lite) | per-model requests/minute and /day (Google AI Studio) | local RPM/RPD guards (`GEMINI_RPM`, `GEMINI_RPD`), day rolls over at Pacific midnight like Google's; one media call covers all readers' languages; caches |
| DeepL API Free | 1,000,000 characters/month | stops at `DEEPL_MONTHLY_CHAR_BUDGET` (950k) and routes to Gemini |
| Telegram | undocumented limits for ephemeral messages | per-chat and global send pacing (`EPHEMERAL_PER_CHAT_PER_MIN`, `GLOBAL_SENDS_PER_S`) |

Voice/video/audio longer than `MAX_MEDIA_SECONDS` (60 s) are skipped. Free-tier providers may use submitted text
to improve their services — `/privacy` tells users so.

## 9. Troubleshooting

| Log line | Meaning |
|---|---|
| `Failed to fetch updates … Cannot connect to host api.telegram.org` | network outage; the bot retries every ~5 s and logs `Connection established` when back. Messages sent meanwhile are delivered after reconnecting (Telegram keeps them 24 h). |
| `configured Gemini model not available for this key` | change `GEMINI_*_MODEL` to a model your key lists |
| `DeepL unavailable at startup` | bad/expired DeepL key — the bot continues on Gemini only |
| `media skipped: too long` | voice/video longer than `MAX_MEDIA_SECONDS` |
| `media: nothing to translate` | no text/speech found (or instrumental music), with the model's confidence |
| `job cancelled unexpectedly` / `job timeout` | a translation exceeded its time budget; the worker keeps running |
| `unhealthy, exiting for restart` | a health problem persisted (dead worker / database); the bot exits non-zero and Docker/systemd restart it |
| exit code 2 at start | invalid configuration; the message names the setting |
