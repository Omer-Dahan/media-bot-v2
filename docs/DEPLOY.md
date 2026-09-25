# Deployment and Cutover Guide

This document covers everything needed to run media-bot-v2 on the production
server: prerequisites, installation, the external tools yt-dlp needs that are
not Python packages, `.env` configuration, the preflight check, and the
step-by-step cutover from the old bot (`media-downloader-bot`) to this one.

See `README.md` for local development and `spec/SPEC.md` section 6 for the
original cutover design this guide implements.

## 1. Prerequisites

- Python 3.13 and `uv` (same as local development).
- MySQL reachable with the same credentials/schema the old bot uses - this
  bot reads and writes the same `users`, `settings`, `payments`, and
  `video_cache` tables (see `media_bot_v2/db/models.py` module docstring).
- `ffmpeg` and `ffprobe` on PATH (used for stream splitting/probing; see
  `media_bot_v2/upload/splitter.py`).
- A JavaScript runtime for yt-dlp (Node.js >= 22, Deno >= 2.3, or Bun) - see
  section 2.
- systemd, if using the sample unit in `deploy/`.

## 2. Server-side tools (not Python packages, not installed by `uv sync`)

### 2.1 JavaScript runtime for yt-dlp

yt-dlp needs a JS runtime to solve YouTube's n-challenge signatures via the
`yt-dlp-ejs` package (already a pinned dependency in `pyproject.toml`, so
`uv sync` installs it - no separate step needed for that package itself).
Install one of:

**Node.js (>= 22)**, via NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version   # expect v22.x or newer
```

**Deno (>= 2.3)**, via the official installer:

```bash
curl -fsSL https://deno.land/install.sh | sh
# add $HOME/.deno/bin to PATH for the shell that installed it, and to the
# systemd unit's Environment=PATH (see deploy/download-bot-v2.service)
deno --version
```

Either is sufficient; `media_bot_v2/engines/youtube.py`'s `js_runtimes`
option tries Deno then Node.js by default (`DEFAULT_JS_RUNTIMES`).

`media_bot_v2.preflight` checks that at least one of `deno`/`node`/`bun` is
on PATH and that `yt_dlp_ejs` is importable - run it after this step (see
section 4).

### 2.2 PO token provider (optional, recommended for reliability)

YouTube increasingly requires a PO (proof-of-origin) token for some player
clients. There are two ways to supply one; pick one:

**Option A: self-hosted PO token provider (recommended, auto-refreshing).**
[`bgutil-ytdlp-pot-provider`](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
runs a small local HTTP server that generates tokens on demand. Run the
server via Docker:

```bash
docker run --name bgutil-provider -d -p 4416:4416 --init --restart unless-stopped \
  brainicism/bgutil-ytdlp-pot-provider
```

Then install the yt-dlp plugin that talks to it, into this project's venv:

```bash
uv add bgutil-ytdlp-pot-provider
```

This is the one exception to "no new Python dependencies" for this
project: it is an opt-in yt-dlp plugin for PO token generation, added only
if you choose Option A, and only at actual deploy time - it is not part of
the M3.1 change set. With the plugin installed and the server running, set:

```
POTOKEN_PROVIDER_URL=http://127.0.0.1:4416
```

so `media_bot_v2.preflight` can verify the server responds before cutover.
Leave `POTOKEN` unset in this mode - the plugin supplies tokens to yt-dlp
directly.

**Option B: a manually obtained static token.** Extract a PO token from a
browser session (see yt-dlp's
[PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)) and
set `POTOKEN=<token>` in `.env`. These tokens expire and need periodic
manual refresh, so prefer Option A for a long-running server.

If neither is configured, YouTube downloads still work for most videos but
may fail with a "PO token required" error on some (classified and
surfaced in Hebrew by `classify_youtube_error`).

## 3. Installing the project

```bash
git clone <this repo> /opt/media-bot-v2   # or your chosen path
cd /opt/media-bot-v2
uv sync
cp .env.example .env
```

Fill in `.env` (see section 5). Do not commit it; it is gitignored.

## 4. Running the preflight check

Before ever pointing this bot at production, run:

```bash
uv run python -m media_bot_v2.preflight
```

This is read-only against the database (with one narrow exception: it
creates the v2-only `provider_health` table if missing, never touching the
shared legacy tables) and never contacts Telegram. It checks:

- The DB is reachable and the expected legacy tables exist.
- `SESSION_NAME` does not collide with the old bot's `main.session`.
- A JS runtime for yt-dlp is on PATH, and `yt-dlp-ejs` is importable.
- The PO token provider server responds, if `POTOKEN_PROVIDER_URL` is set
  (skipped otherwise).
- `ffmpeg` and `ffprobe` are present and runnable.
- `DOWNLOAD_DIR` and the log directory are actually writable.
- `provider_health` exists (creating it if this is the first run against
  this database).

It prints one line per check (`PASS`/`FAIL`/`SKIP`) and exits non-zero if
anything failed. Fix every `FAIL` before proceeding to cutover.

## 5. `.env` configuration

Copy `.env.example` to `.env` and fill in every value; see that file's
comments for what each variable does. The values that matter most for a
first deployment:

- `APP_ID` / `APP_HASH` / `BOT_TOKEN`: same Telegram app/bot as the old bot.
  **Never run this bot and the old bot against the same `BOT_TOKEN`
  simultaneously** - see section 6.
- `SESSION_NAME`: must stay different from the old bot's `main` (enforced
  at startup; `Settings` raises if you set it to `main`).
- `DB_DSN`: the production MySQL DSN, e.g.
  `mysql+pymysql://user:password@host/dbname`.
- `MB_PER_CREDIT` (default `200`, same as the old bot): credits are charged
  by volume, `max(1, ceil(total_delivered_MB / MB_PER_CREDIT))` per request.
  200MB -> 1, 400MB -> 2, 5GB -> 26. Change it (e.g. `100`) here, no code
  change needed; restart the service to apply. Must be a positive integer.
- `UPLOAD_WORKERS` (default `5`, range `1..5`): how many parts of one file are
  uploaded to Telegram at the same time. Telegram limits bandwidth per
  connection stream, so several parallel parts make large uploads much
  faster (Telethon's own `upload_file` has no such option and sends parts one
  by one, so `media_bot_v2/telegram/parallel_upload.py` does it). Values above
  5 are clamped to 5; below 1 the bot refuses to start. `1` = the old
  sequential behaviour. **Warning:** more lanes means more requests per
  second and a higher `FLOOD_WAIT` risk. A flood wait retries only the part
  that hit it; repeated ones automatically drop the lanes (5 -> 2 -> 1) and
  the upload continues. If you still see `Flood wait` warnings in the log,
  lower this value. The real speed-up depends on your server's bandwidth
  and Telegram's limits for the account/DC.
- `YOUTUBE_COOKIES_FILE` / `TIKTOK_COOKIES_FILE`: paths to cookie files, if
  you use them (never commit these files).
- `TIKTOK_PROVIDERS` / `YOUTUBE_PROVIDERS` / `DISABLED_PROVIDERS`: provider
  fallback order, see `docs/providers.md`.

## 6. systemd service

Copy the sample unit and edit the `CHANGE-ME` placeholders (project path,
venv path, and the JS runtime's PATH entry):

```bash
sudo cp deploy/download-bot-v2.service /etc/systemd/system/
sudo nano /etc/systemd/system/download-bot-v2.service   # fill in CHANGE-ME spots
sudo systemctl daemon-reload
sudo systemctl enable download-bot-v2.service
```

Do not `systemctl start` it yet - see the cutover steps below.

### 6.1 Reading the logs

The bot logs to two places, with the same JSON-lines format and level (`INFO`):

| Where | How to read | Notes |
|-------|-------------|-------|
| systemd journal (stdout) | `journalctl -u download-bot-v2 -f` | Live tail; survives file rotation; filter with `--since "1 hour ago"` or `-p warning`. Journal retention is governed by journald, not the bot. |
| `logs/bot.log` | `tail -f logs/bot.log` | Rotates by `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` (`bot.log.1`, ...). Per-request logs are under `logs/requests/`. |

Set `LOG_TO_CONSOLE=false` in `.env` to disable the stdout copy (the file is
always written). The unit sets `PYTHONUNBUFFERED=1` so lines reach the journal
immediately. Output contains no ANSI colour codes.

`telethon`, `httpx`, `httpcore` and `urllib3` are held at `WARNING` and above
to keep the journal readable; the bot's own loggers stay at `INFO`.

## 7. Cutover plan

The goal: swap the running process without creating a second MTProto
session consumer against the same bot token, and without corrupting either
bot's session file or losing in-flight user data.

1. **Confirm readiness first.** Run the preflight check (section 4) against
   the production `.env` and fix every failure. Do this well before the
   cutover window, not during it.

2. **Announce/plan a short downtime window.** Users mid-download on the old
   bot will see it stop responding for the duration of the swap.

3. **Stop the old bot.**
   ```bash
   sudo systemctl stop <old-bot-service-name>
   ```
   Confirm it actually exited (`systemctl status`, and that its process is
   gone from `ps`) before continuing - Telegram allows only one active
   MTProto session consumer per bot token, and starting the new bot while
   the old one is still attached will make one of them lose its connection
   unpredictably.

4. **Start the new bot** with the production `BOT_TOKEN` and `DB_DSN` now
   in its `.env`:
   ```bash
   sudo systemctl start download-bot-v2.service
   sudo systemctl status download-bot-v2.service
   ```

5. **Verify immediately:**
   - `journalctl -u download-bot-v2.service -f` shows "Starting
     media-bot-v2" with no startup exceptions.
   - Send `/ping` from a personal Telegram account and confirm a reply.
   - Send `/start` from an account that already exists in the DB and
     confirm it responds normally (proves the DB connection and existing
     user data are intact).
   - Send one direct-link URL, one TikTok URL, and one YouTube URL (small
     files) and confirm each downloads and uploads successfully - this
     exercises the DB, the provider layer, ffmpeg, and the JS runtime in
     one pass.
   - Check `logs/bot.log` (or `journalctl`) for unexpected warnings,
     especially the "external extraction providers are DISABLED" warning
     from `media_bot_v2/telegram/router.py` - it must not appear in
     production (see README "Provider registry default").

6. **Keep the old bot's service and code in place, disabled, for a
   rollback window** (a few days is reasonable) rather than deleting
   anything immediately.

## 8. Rollback

If step 5 turns up a serious problem:

```bash
sudo systemctl stop download-bot-v2.service
sudo systemctl start <old-bot-service-name>
```

Since both bots read/write the same DB and the new bot only adds the
`provider_health` table (never altering shared tables), stopping the new
bot and restarting the old one requires no data migration or schema
rollback - confirm the old bot starts cleanly and resumes serving users
normally.
