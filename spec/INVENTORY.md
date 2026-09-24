# Old bot inventory (media-downloader-bot)

Read-only audit of `/home/vm/projects/media-downloader-bot` as of 2026-09-17
(git HEAD `2c7190a`). Every item is tagged:

- ✅ **keep** — carry the behavior/content over as-is
- 🔧 **rebuild** — same idea, new implementation (different library, fixed bug, or async)
- ❌ **drop** — out of v1 scope, dead code, or not worth carrying forward

The old bot uses **Kurigram** (`kurigram==2.2.15`, imported as `pyrogram`), not
vanilla Pyrogram. This confirms the locked decision to move to Telethon rather
than staying on a Pyrogram-family fork.

## 1. Database schema

Verified against `src/database/model.py`, `src/database/cache.py`, and the
actual DDL in the repo's local `database.sqlite3` (empty dev copy — 0 rows in
every table; production data lives on the server via `DB_DSN`, most likely
MySQL given `PyMySQL` in requirements.txt and the MySQL-specific `JSON`
column import in the old model).

| Table | Columns | Notes |
|---|---|---|
| `users` | `id` PK, `user_id` BIGINT UNIQUE NOT NULL, `first_name` VARCHAR(100), `username` VARCHAR(100), `free` INT, `paid` INT, `bandwidth_used` BIGINT, `total_bandwidth` BIGINT, `is_blocked` INT, `config` JSON | ✅ keep exactly |
| `settings` | `id` PK, `quality` ENUM(high,medium,low,audio,custom) VARCHAR(6), `format` ENUM(video,audio,document) VARCHAR(8), `subtitles` INT, `title_length` INT, `user_id` FK→users.id | ✅ keep exactly |
| `payments` | `id` PK, `method` VARCHAR(50), `amount` FLOAT, `status` ENUM(pending,completed,failed,refunded) VARCHAR(9), `transaction_id` VARCHAR(100), `user_id` FK→users.id | ✅ keep exactly |
| `video_cache` | `id` PK, `cache_key` VARCHAR(64) UNIQUE INDEX, `file_id` TEXT (JSON list), `meta` TEXT (JSON), `created_at` DATETIME | ✅ keep exactly — despite living in a module called `cache.py`/class `Redis`, it is plain SQLite/MySQL, no actual Redis dependency |

Reproduced in `media_bot_v2/db/models.py`; schema parity is enforced by
`tests/test_models_match_old_schema.py`.

Two bugs worth fixing in the rewrite (not schema changes, just app-layer bugs):
- `config.py`'s `DB_DSN` is imported but never used for the actual connection
  — `model.py` and `cache.py` each re-read `os.getenv("DB_DSN")` directly with
  their own inline defaults. 🔧 fix: single source of truth (done in
  `media_bot_v2/db/session.py`, fed from `Settings.db_dsn`).
- `LOG_FILE` is defined in two places (`config/config.py` and
  `config/__init__.py`) with an extra `BOT_LOG_FILE` alias in the latter. 🔧
  fix: one `LOG_FILE` var, no alias.

## 2. Credits and quota logic

Source of truth: `src/database/model.py` (functions) + `src/engine/base.py`
`BaseDownloader.start()`/`_upload()` (call sites). Full call-site sequence
already traced by the background recon agent; summary:

| Behavior | Keep/rebuild | Detail |
|---|---|---|
| Deduct free credits before paid | ✅ keep | `use_quota_dynamic`, `model.py:272-321` |
| 1 credit per 200MB, rounded up, min 1 if any bytes | ✅ keep | same file |
| Owners (`OWNER` env) exempt from all checks | ✅ keep | `check_quota`, `model.py:237-238` |
| `ENABLE_VIP=false` → unlimited, no-op | ✅ keep | every quota function short-circuits |
| Daily bandwidth cap (`FREE_BANDWIDTH`, paid users exempt) | ✅ keep | `model.py:250-253` |
| Credits deducted **after** successful upload (normal path) | ✅ keep | `base.py:1195-1217` |
| Credits deducted **before** split+upload for large non-MKV video | 🔧 fix | `base.py:976` — the one path where a failed send after ffmpeg-split can charge a user with no file delivered. No refund path exists anywhere in the codebase. New bot: charge after success, or add an explicit refund on failure. |
| Cache-hit re-download comment says "still deduct credits (minimal cost)" but `_record_usage(0)` actually deducts 0 | 🔧 fix | `base.py:1244-1245` — either make the comment true or make the behavior match the comment; decide deliberately, don't silently inherit the mismatch |
| `is_blocked` raises a bare `Exception`, not a typed one | 🔧 fix | `model.py:244-245` — use a dedicated `UserBlockedException` |
| `use_quota()` (flat 1-credit-per-download, non-size-based) | ❌ drop | dead code, superseded by `use_quota_dynamic`, zero call sites |
| `get_long_description_settings()` always returns `False` | ❌ drop | dead stub, `title_length` already replaced it |
| `credit_account()` (Stripe payment webhook credits `paid`) | 🔧 rebuild if payments are in v1 scope | see open question in SPEC.md — only call site is `successful_payment` handler |

## 3. Menus, buttons, and text (Hebrew, verbatim where quoted)

Full detail from recon agent; high-value items below. All content is in
`src/main.py`, `src/admin.py`, `src/config/constant.py`.

### Commands
`/start`, `/help`, `/about`, `/ping`, `/buy`, `/stats`, `/settings`, `/direct`,
`/spdl`, `/ytdl` (group-only), `/torrent`, `/adminpanel` — registration sites
listed in the recon transcript, file:line preserved there for reference.

**v1 scope note:** per the locked decision (YouTube/TikTok/Instagram/direct
only, no torrents, no JDownloader, no full admin panel), only `/start`,
`/help`, `/about`, `/ping`, `/settings`, `/direct`, `/stats` map cleanly to
v1. `/spdl`, `/torrent`, `/ytdl`, `/adminpanel` are ❌ out of v1 scope as-is.

### `BotText` (`config/constant.py:6-84`) — ✅ keep content, 🔧 rebuild as a proper text module
- `start` (line 8-22), `help` (24-62), `about` (64), `settings` (66-74),
  `youtube_quality_select` (76-83, takes `{title}`/`{duration}`).
- `about`: decision closed — all upstream attribution and repository links
  removed per owner request. The text is now a neutral description of the bot
  and its capabilities. This project is an independent codebase and not a fork
  or version of the original project.

### Settings menu (`_build_settings_markup`, `main.py:850-896`) — ✅ keep UX, 🔧 rebuild for Telethon
Toggle-style buttons cycling in place (not a full menu tree):
`toggle_quality` (high→medium→low→high), `toggle_format`
(video↔document↔audio), `toggle_subtitles` (on/off), `toggle_title_len`
(100→250→500→1000→4000→unlimited→100). Good pattern, worth keeping —
minimizes taps, single message edited in place.

### YouTube quality-select menu (`main.py:668-693`) — ✅ keep UX, 🔧 rebuild storage
Buttons: 1080p / 720p / 480p / 360p / audio-only, `callback_data=f"ytq:{quality}:{url_hash}"`.
The `url_hash → url` mapping lives in an unbounded, non-expiring, in-process
`dict` (`_youtube_url_cache`, `main.py:88`) — entries leak if never clicked
and are lost on restart. 🔧 rebuild with a TTL-bounded store.

### Admin panel (`src/admin.py`, 740 lines) — ❌ out of v1 scope
Full featureset (ping, server/download stats, paginated user lists, add
credits, reset quota, block user, yt-dlp self-update + process restart,
clear cache, JDownloader launcher) documented in full by the recon agent.
Per the locked decision, v1 ships without a full admin panel. Two items are
worth flagging regardless of admin-panel timing:
- `admin:user_action:{user}:{action}` dispatcher exists (`admin.py:134-137,
  373-390`) but **no button anywhere emits that callback_data** — the user
  list has no per-row action buttons. ❌ dead/unreachable code, don't port.
- The yt-dlp self-update flow restarts the whole process via
  `subprocess.Popen([sys.executable]+sys.argv)` + `os._exit(0)`
  (`admin.py:596-603`). ❌ drop this pattern — a systemd-managed process
  should be restarted by systemd, not by re-executing itself.

### State management — 🔧 rebuild as a structured module
Old bot uses five separate in-process, unpersisted, module-level
dicts/sets with no shared abstraction: `_admin_state`, `_youtube_url_cache`,
`_torrent_waiting_users`, `cancellation_events`, `_resume_state_cache`. All
are lost on restart (acceptable for a single-process bot) but none have
TTL/bounds. ✅ keep "in-memory is fine for a single process" as the design
choice for v1; 🔧 rebuild as one typed, bounded state module instead of five
ad hoc globals.

## 4. Download engines

| Engine | v1 scope? | State | Verdict |
|---|---|---|---|
| `generic.py` (YouTube + generic yt-dlp fallback) | ✅ yes | actively wired, well maintained, one harmless unreachable `quality=="custom"` branch | 🔧 rebuild for asyncio, this **is** the M2 YouTube engine |
| `direct.py` (HTTP direct links, aria2c optional) | ✅ yes | actively wired, solid | 🔧 rebuild for asyncio |
| `tiktok.py` (yt-dlp + gallery-dl slideshow + tiktokapipy) | ✅ yes | actively wired; `vm.tiktok.com` missing from `DOWNLOADER_MAP` so it silently bypasses TikTok-specific handling (real bug) | 🔧 rebuild for asyncio, fix the `vm.tiktok.com` gap |
| `instagram.py` (yt-dlp with curl-cffi impersonation) | ✅ yes | implemented in `media_bot_v2/engines/instagram.py` | ✅ implemented for asyncio; local yt-dlp with curl-cffi impersonation is default without cookies, optional `INSTAGRAM_COOKIES_FILE` |
| `pixeldrain.py`, `krakenfiles.py` (small direct-link specializations) | ❌ no | working, actively wired, but not in the named v1 platform list | ❌ defer past v1 (or fold trivially into the direct-link engine later) |
| `reddit.py` | ❌ no | working, depends on optional `RedDownloader` lib | ❌ defer past v1 |
| `googledrive.py` | ❌ no | **fully implemented but never imported anywhere** — `DOWNLOADER_MAP` routes Google Drive links to a stub that says "temporarily disabled" instead | ❌ drop — dead code, and out of v1 scope anyway |
| `torrent.py`, `torrent_manager.py` (qBittorrent) | ❌ no | working, well engineered | ❌ out of v1 scope per locked decision |
| `jdownloader.py`, `jdownloader_manager.py` | ❌ no | working, well engineered, largest single subsystem (1155 lines combined) | ❌ out of v1 scope per locked decision |
| `archive_manager.py` (ZIP/split for torrent+jdownloader) | ❌ no | only consumed by torrent/jdownloader | ❌ drop with them — **not** the same mechanism as the Telegram-2GB video splitter in `base.py`, which is kept (see below) |
| `concurrency.py` (per-user free=1/paid=6 slots + global cap) | ✅ yes | actively used, works | 🔧 rebuild with `asyncio.Semaphore` instead of thread-based locking |
| `helper.py` grab-bag | mostly ✅ | `moon_progress_bar`, `safe_truncate`, `get_user_display_name`, `create_telegraph_page`, `extract_metadata_from_info`, `handle_download_error` all actively used | ✅ keep the useful ones, ❌ drop `debounce()` (confirmed zero call sites) |
| `network_errors.py` (classifier + resume-download button) | partial | working, but resume adds real complexity (persistent partial-download state) | 🔧 keep error classification; resume-button UX is an **open question** — consider simplifying to plain retry for v1 |
| `request_logger.py` (per-request log capture, redacted) | ✅ yes, as concept | working | 🔧 rebuild as part of the new structured-logging module rather than a bespoke `ContextVar` system |
| `base.py` (abstract downloader: cache check → download → upload → split → archive → credit deduction) | ✅ yes, as architecture | this **is** the reference flow for the new engine base class | 🔧 rebuild for asyncio/Telethon, fixing the pre-charge-before-split bug above |

Also: the 2GB-per-file Telegram upload splitter (`base.py:700-890`,
ffmpeg-based, ~1.9GB parts, caption relabeling) is a **different mechanism**
from `archive_manager.py`'s ZIP splitting used by torrent/JDownloader. ✅ keep
the ffmpeg video-splitter; ❌ drop the ZIP splitter along with torrent/JDownloader.

## 5. `.env` variables (names only — no `.env` file was read; it does not
exist in this repo checkout, confirming it lives only on the server)

| Variable | v1 relevance | Default in old code |
|---|---|---|
| `APP_ID`, `APP_HASH`, `BOT_TOKEN`, `OWNER` | ✅ required | none (required) |
| `DB_DSN` | ✅ required | `sqlite:///database.sqlite3` |
| `ENABLE_VIP` | ✅ | none (falsy) |
| `FREE_DOWNLOAD` | ✅ | `3` |
| `FREE_BANDWIDTH` | ✅ | `2147483648` (2GB) |
| `ARCHIVE_CHANNEL` | ✅ | none |
| `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | ✅ | `logs/bot.log`, `10485760`, `5` |
| `WORKERS` | ✅ (global concurrency cap) | `100` |
| `ENABLE_FFMPEG`, `AUDIO_FORMAT`, `M3U8_SUPPORT`, `ENABLE_ARIA2`, `TMPFILE_PATH` | ✅ (used by direct/YouTube engines) | `None`/`"m4a"`/`None`/`None`/`None` |
| `BROWSERS`, `POTOKEN` | ✅ **critical for YouTube** (see SPEC.md environment section) | none |
| `INSTAGRAM_SESSION_FILE`, `INSTAGRAM_COOKIES_FILE`, `TIKTOK_COOKIES_FILE` | ✅ (cookies for IG/TikTok engines) | none |
| `TOKEN_PRICE`, `PROVIDER_TOKEN` | open question (payments in v1?) | `10` / none |
| `AUTHORIZED_USER` | 🔧 keep concept, review semantics | `""` |
| `REQUEST_LOG_DIR` | 🔧 folded into new logging module | `logs/requests` |
| `QBITTORRENT_*`, `TORRENT_*` | ❌ unused in v1 | various |
| `JDOWNLOADER_*` | ❌ unused in v1 | various |

## 6. Summary judgment

The old bot's **data model, credit math, and settings UX are sound** and
should be preserved close to verbatim. The **engine layer for YouTube,
direct links, TikTok, and Instagram is functionally complete** and is the
right reference to port to asyncio — it is not being rebuilt from a blank
page, it is being translated. The biggest real bug worth fixing during the
rewrite (not a matter of taste) is the pre-charge-before-split credit gap in
`base.py:976`. The biggest pile of dead weight is `googledrive.py` (fully
built, never wired in) and the unreachable `admin:user_action` dispatcher —
neither should be carried forward. Torrents, JDownloader, and the full admin
panel are cleanly out of v1 scope per the locked decisions and every engine
file behind them can be left behind entirely for this stage.
