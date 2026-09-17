# media-bot-v2

Telegram media download bot (YouTube, TikTok, Instagram, direct links) built on Telethon.
Ground-up rewrite of the previous bot (`media-downloader-bot`), keeping the same bot
token and the same production database so existing users, credits, and payment
history carry over unchanged. See `spec/SPEC.md` for the full design, module
breakdown, milestones, and cutover plan.

## Status

Stage 0: spec and code skeleton only. No download engines are implemented yet.
The bot must not be started while the old bot is running with the same token.

## Stack

Python 3.13, Telethon, yt-dlp, SQLAlchemy 2.0, pydantic-settings, uv. See
`spec/SPEC.md` for version rationale.

## Development

Dependencies are declared in `pyproject.toml` but are not installed as part of
this stage. Once approved:

```bash
uv sync
uv run pytest
```

Tests never connect to Telegram and never touch the production database -
they run against an in-memory SQLite DB and mock the Telethon client.

## Layout

```
media_bot_v2/
  config.py          # typed settings loaded from .env
  logging_setup.py    # rotating structured file logs
  bootstrap.py         # entrypoint wiring (not run at import time)
  db/                  # SQLAlchemy models (same schema as the old bot) + session
  credits/             # credit/quota service ported from the old bot's logic
  telegram/            # Telethon client bootstrap, router, callback_data helpers
  engines/             # per-platform download engines (M2+)
  queue/                # concurrency limiting
  upload/               # large-file splitting for uploads over 2GB
tests/                  # pytest, no live Telegram/DB connections
spec/SPEC.md            # full specification
```
