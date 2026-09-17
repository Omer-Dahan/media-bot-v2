# media-bot-v2

Telegram media download bot (YouTube, TikTok, Instagram, direct links) built on Telethon.
Ground-up rewrite of the previous bot (`media-downloader-bot`), keeping the same bot
token and the same production database so existing users, credits, and payment
history carry over unchanged. See `spec/SPEC.md` for the full design, module
breakdown, milestones, and cutover plan, and `spec/INVENTORY.md` for the old-bot
audit this rewrite is ported from.

## Status

M1: skeleton, DB schema parity, credits/quota logic, the Telethon command/settings
UI, and one download engine (direct HTTP(S) links) implemented end-to-end -
download, split over 2GB, upload, credit deduction, disk cleanup. YouTube, TikTok,
and Instagram engines are UI-only stubs; the real engines land in M2/M3.

**The bot has not been started and has not connected to Telegram at any point
during this milestone.** The old bot is running in production against the same
bot token; starting this bot concurrently would create a second MTProto session
consumer against that token. See "Running the bot" below before ever starting it.

## Stack

Python 3.13, Telethon, yt-dlp, SQLAlchemy 2.0, pydantic-settings, uv. See
`spec/SPEC.md` for version rationale.

## Setup

```bash
uv sync
cp .env.example .env   # fill in APP_ID/APP_HASH/BOT_TOKEN/OWNER for a TEST bot, never the production one
```

## Running the tests

```bash
uv run pytest -q
```

Tests never connect to Telegram (Telethon clients are built against an in-memory
session or mocked) and never touch the production database - they run against
SQLite (in-memory or a tmp file). A local `http.server` fixture stands in for the
direct-link engine's target so `test_direct_engine.py` proves the full streaming
download path without touching the real internet. `test_splitter.py` uses a real
ffmpeg binary to generate and split a short throwaway test clip (also local-only).

### MySQL schema compatibility

`tests/test_mysql_schema_compat.py` compiles every model's DDL against
`sqlalchemy.dialects.mysql` (`CreateTable(...).compile(dialect=mysql.dialect())`)
and checks it against the old bot's table/column names - this catches
MySQL-dialect issues (enum rendering, VARCHAR lengths, reserved words) without
needing a MySQL server, and runs as part of the normal `pytest` invocation above.

`tests/test_mysql_integration.py` runs the same models against a **real** MySQL
server and is skipped unless `MYSQL_TEST_DSN` is set. Point it at a throwaway
database, never production:

```bash
MYSQL_TEST_DSN="mysql+pymysql://user:pass@host/throwaway_db" uv run pytest tests/test_mysql_integration.py -v
```

This creates all four tables, does a basic insert/read round trip through the
`User` model, then drops the tables again. It never reads `DB_DSN` from `.env`.

## Running the bot

Not part of this milestone - do not start it. When you do (M2+, against a
**test** bot token only):

```bash
uv run python -m media_bot_v2.bootstrap
```

Two things must hold before ever starting this bot:

1. **`BOT_TOKEN` must be a test bot's token, never the production token**, as
   long as the old bot (`media-downloader-bot`) is still running - Telegram
   allows only one active session consumer per bot token, and Telethon and
   the old bot's Kurigram client would fight over it.
2. **`SESSION_NAME` (default `v2`) must stay different from the old bot's
   session name (`main`, i.e. `main.session`)**, even after cutover. Telethon
   session files are self-contained MTProto login state; starting this bot
   with the old bot's session file while the old bot is running (or vice
   versa) invalidates that shared session for whichever client loses the
   race.

## Cutover plan

Summarized here; full detail (including the read-only production dry-run step)
is in `spec/SPEC.md` section 6. The short version: develop and test against a
separate test bot token through M1-M3, never run both bots against the
production token at once, stop the old bot's process before pointing this bot
at the production `BOT_TOKEN`/`DB_DSN`, and keep the old bot's code and systemd
unit in place (disabled) for a rollback window after cutover.

## Layout

```
media_bot_v2/
  config.py             # typed settings loaded from .env
  logging_setup.py       # rotating structured file logs
  bootstrap.py            # entrypoint wiring (not run at import time)
  pipeline.py              # download -> split -> upload -> charge -> cleanup
  db/                       # SQLAlchemy models (same schema as the old bot) + session
  credits/                  # credit/quota service ported from the old bot's logic
  telegram/                 # Telethon client bootstrap, router, menus, callback_data
  engines/                  # per-platform download engines (direct done, rest M2+)
  queue/                    # concurrency limiting
  upload/                   # large-file splitting for uploads over 2GB
tests/                      # pytest, no live Telegram/DB connections
spec/SPEC.md                # full specification
spec/INVENTORY.md           # old-bot audit (keep/rebuild/drop per item)
```

## Design decisions worth knowing

- **Credits are charged only after a fully successful upload**, never before.
  The old bot charged credits before splitting+uploading large videos
  (`src/engine/base.py:976`), so a failed send after a successful ffmpeg split
  still cost the user credits with no refund path anywhere in that codebase.
  See `media_bot_v2/pipeline.py`'s module docstring for the full reasoning; in
  short, charge-after-success needs exactly one accounting write gated on
  total success, instead of a charge-then-refund design that needs a second
  write to itself succeed for the accounting to stay correct.
- **A blocked user (`is_blocked`) now raises a dedicated `UserBlockedException`**
  instead of the old bot's bare `Exception` (`src/database/model.py:244-245`).
- Progress is reported through **one message, edited in place**, never a
  stream of new messages per download phase.
