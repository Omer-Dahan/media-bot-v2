# CLAUDE.md - agent memory for media-bot-v2

## What this is
Telegram media downloader bot. Telethon only (locked decision, not Kurigram/Pyrogram/aiogram).
Python 3.13 + uv. MySQL in prod, SQLite in tests.

## Map
- `media_bot_v2/bootstrap.py` -> process entrypoint, `main()` under `if __name__ == "__main__"`
- `media_bot_v2/config.py` -> settings (pydantic-settings)
- `media_bot_v2/pipeline.py` -> core download/convert/deliver orchestration
- `media_bot_v2/executor.py` -> task execution glue
- `media_bot_v2/preflight.py` -> read-only cutover readiness checks, run via `python -m media_bot_v2.preflight`
- `media_bot_v2/engines/base.py` -> engine interface
- `media_bot_v2/engines/{youtube,tiktok,instagram,facebook,direct}.py` -> per-source download engines
- `media_bot_v2/engines/ssrf_guard.py` -> blocks internal/unsafe URLs before direct download
- `media_bot_v2/engines/safe_filename.py` -> sanitizes delivered file names
- `media_bot_v2/engines/content_check.py` -> pre-download content validation
- `media_bot_v2/engines/permissions.py` -> engine-level permission checks
- `media_bot_v2/providers/registry.py` -> provider registration/lookup
- `media_bot_v2/providers/health.py` -> provider health tracking
- `media_bot_v2/providers/{cobalt,downloader,musicaldown,tikdownloader,tikwm,ytmp3}.py` -> external fallback providers
- `media_bot_v2/telegram/router.py` -> command/callback routing
- `media_bot_v2/telegram/progress.py` -> single progress message per download, edited in place
- `media_bot_v2/telegram/progress_format.py` -> progress text formatting
- `media_bot_v2/telegram/delivery.py` -> final file delivery to chat
- `media_bot_v2/telegram/uploader.py` -> upload execution
- `media_bot_v2/telegram/parallel_upload.py` -> parallel part upload for split files
- `media_bot_v2/telegram/flood_wait.py` -> FloodWait handling
- `media_bot_v2/telegram/retry.py` -> hopeless-failure classification, retry policy
- `media_bot_v2/telegram/error_report.py` -> error reporting to user/admin
- `media_bot_v2/telegram/texts.py` -> all user-facing Hebrew text, variant resolution
- `media_bot_v2/telegram/settings_menu.py`, `quality_menu.py` -> inline menus
- `media_bot_v2/telegram/captions.py` -> caption building
- `media_bot_v2/telegram/callback_data.py` -> callback_data encode/decode (bytes)
- `media_bot_v2/upload/splitter.py` -> splits files over Telegram's 2GB limit
- `media_bot_v2/upload/media_probe.py` -> ffmpeg-based media inspection
- `media_bot_v2/upload/audio_converter.py` -> audio format conversion
- `media_bot_v2/upload/streamable.py` -> streamable-format checks
- `media_bot_v2/credits` -> credit charging (post-delivery)
- `media_bot_v2/cache` -> cache-hit lookups
- `media_bot_v2/db` -> DB models/session (MySQL prod, SQLite tests)
- `media_bot_v2/queue` -> request queue/limiter
- `media_bot_v2/logging_setup.py` -> logging config

## Request flow
1. `telegram/router.py` receives the command/callback
2. `queue/` limiter admits the request
3. `engines/*` download via the matched engine (SSRF guard, safe filename, content check run first)
4. `pipeline.py` orchestrates conversion/probing (`upload/media_probe.py`, `upload/audio_converter.py`)
5. `upload/splitter.py` splits if over 2GB; `telegram/parallel_upload.py` uploads parts
6. `telegram/delivery.py` + `telegram/uploader.py` deliver the file to the chat
7. `credits/` charges the user only after successful delivery
8. `telegram/progress.py` edits the single progress message throughout, in place

## Invariants (breaking these has caused production bugs)
- Telegram client is Telethon; callback_data are bytes (`telegram/callback_data.py`).
- One progress message per download, edited in place; `_last_text` dedupe (`telegram/progress.py`).
  A failed edit must never turn a delivered download into a "failed" report.
- Clearing an inline keyboard requires `buttons=None` passed explicitly; an empty `ReplyInlineMarkup`
  is rejected by Telegram (`telegram/progress.py`).
- `_is_terminal_text` (`telegram/progress.py`) classifies the CANONICAL stage constants; user-facing
  text is randomized only at the send boundary by `texts.resolve(text, key)` (`telegram/texts.py`),
  keyed per-request so it stays stable across edits.
- `retry.py` decides hopeless failures from canonical texts/exceptions; variant strings must stay
  covered by `texts.ALL_HOPELESS_VARIANTS` (`telegram/retry.py`).
- Credits are charged only after the file is delivered; cache hits are near-instant.
- Files over 2GB (`upload/splitter.py`, `TG_NORMAL_MAX_SIZE`) are split; non-video parts need a join notice.
- SSRF guard, safe filename and content check run before any direct download (`engines/`).
- User-facing Hebrew lives in `telegram/texts.py` only; comments, docstrings and commits are English.

## Commands
- `uv run pytest -q` - full test suite
- `uv run pytest -q path/to/test_file.py::test_name` - single test
- `uv run ruff check` - lint
- `uv run python -m media_bot_v2.preflight` - read-only cutover readiness checks (see `docs/DEPLOY.md`)

## Working with agents on this repo
- Read this map first; do not read the whole repo.
- Tests assert a category (any variant), not a fixed sentence - texts are randomized by `texts.resolve`.
- Commit as soon as tests + ruff are green.
- Run the full suite once at the end and grep the tail, don't paste raw output.

## State
Updated: 2026-10-21
- Done: core pipeline, engines (youtube/tiktok/instagram/facebook/direct), provider fallbacks,
  Telegram delivery/progress/retry, upload splitting, credits, caching, preflight checks.
- Next: Reddit engine missing, E10 Telegraph, `/stats`. See `spec/SPEC.md` for the full backlog.
