# media-bot-v2 — Specification (Stage 0)

Status: draft, stage 0 (spec + skeleton, no download engines implemented).
Companion document: `spec/INVENTORY.md` (full old-bot audit, keep/rebuild/drop
per item). This document defines what gets built and in what order.

## 1. Locked decisions

These were decided before this stage started and are not re-opened here.

| # | Decision |
|---|---|
| 1 | New project in a new repo (`media-bot-v2`). The old bot (`media-downloader-bot`) keeps running in production until cutover. |
| 2 | Same Telegram bot token and same production database as the old bot — same schema, same tables (`users`, `settings`, `payments`, plus `video_cache`), same ID structure. No migration, no data deletion, ever. |
| 3 | Telethon (MTProto) as the Telegram client library. Not Kurigram, not Pyrogram, not aiogram. Telethon's `callback_data` is `bytes`, unlike Pyrogram-family libraries which use `str` — every callback_data builder/parser goes through one helper (`media_bot_v2/telegram/callback_data.py`) to keep this in one place. |
| 4 | v1 scope: YouTube, TikTok, Instagram, and direct links only. No torrents, no JDownloader, no full admin panel. The 2GB Telegram upload split, the archive channel, the download cache, and the credit system must all work because they are part of the core download flow, not optional extras. |
| 5 | Claude writes the code; the user (agy) verifies it. |

## 2. Proposed stack

No installation happened as part of this stage — `pyproject.toml` declares
these but nothing has been run. Versions are floors (`>=`), not exact pins;
the rationale is why each floor is current and trustworthy as of 2026-09.

| Package | Floor | Why |
|---|---|---|
| Python | 3.13 | Matches what the old bot already runs in production (`requirements.txt` pins `Python 3.13.7`), so the cutover server doesn't need a new interpreter. |
| `telethon` | 1.36.0 | Current stable MTProto library for Python; actively maintained, async-native, the locked choice for this project. |
| `yt-dlp[default,curl-cffi]` | 2024.12.0 | YouTube/TikTok/Instagram extraction backbone; the old bot already depends on it and keeps it working via frequent updates — same story here, floor only, expect to bump often. `curl-cffi` extra gives TLS fingerprint impersonation, needed to avoid basic bot detection on some sites. |
| `yt-dlp-ejs` | 0.1.0 | yt-dlp's JS-challenge-solving plugin (n-parameter/PO token challenges). The old bot already carries this; YouTube stopped being reliably downloadable without a JS runtime behind yt-dlp, see §4. |
| `sqlalchemy` | 2.0.36 | Same major version the old bot already uses against the same database; 2.0's typed `Mapped[...]` style (used in `media_bot_v2/db/models.py`) is the current idiomatic SQLAlchemy API, not a new dependency risk. |
| `pymysql` | 1.1.1 | Pure-Python MySQL driver, matches the old bot's driver so the same `DB_DSN` connects the same way in production. |
| `pydantic-settings` | 2.6.0 | Typed, validated env-var loading with `.env` support; replaces the old bot's hand-rolled `get_env()` string-coercion helper (`src/config/config.py:5-16`) with something that fails fast on missing/malformed required vars instead of silently returning `None`. |
| `ffmpeg-python` | 0.2.0 | Thin wrapper the old bot already uses for probing and splitting; ffmpeg itself is a system binary, not a Python package (see §4). |
| `filetype` | 1.2.0 | Magic-byte MIME sniffing for picking the right Telegram send method (video/audio/photo/document), same library the old bot uses. |
| `pytest`, `pytest-asyncio` | 8.3.0 / 0.24.0 | Standard async-aware test stack; Telethon's client and the engines are async, so tests need `pytest-asyncio`. |
| `ruff` | 0.8.0 | Single fast linter/formatter, replaces needing both a linter and a formatter as separate tools. |
| `uv` | (user-managed, not a dependency) | Fast, lockfile-based Python package manager; the user runs `uv sync` / `uv run` once dependencies are approved — this stage does not invoke it. |

Deliberately **not** carried forward from the old stack: `instaloader`,
`gallery-dl`, `qbittorrent-api`, `myjdapi`, `token-bucket`, `APScheduler`,
`fakeredis`/`redis` (the "Redis" cache is plain SQL, see `INVENTORY.md` §1),
`tqdm`, `psutil`, `tgcrypto` (Telethon has its own crypto backend). Some of
these come back in M2/M3 when the corresponding engine is built (e.g.
`instaloader` when the Instagram engine lands) — they are not rejected, just
not declared until the milestone that needs them, per the "don't install
ahead of need" constraint for this stage.

## 3. Module breakdown

Matches the skeleton already written under `media_bot_v2/`:

| Module | Responsibility | Status |
|---|---|---|
| `config.py` | Typed settings from env/`.env`, same variable names as the old bot where reused (§ old-bot `.env` table in `INVENTORY.md`) | skeleton done (core fields; engine-specific vars added per milestone) |
| `logging_setup.py` | Structured (JSON-lines) logging to a rotating file, `LOG_FILE`/`LOG_MAX_BYTES`/`LOG_BACKUP_COUNT` | skeleton done |
| `telegram/client.py` | Telethon client construction from config; never connects at import time | skeleton done |
| `telegram/callback_data.py` | Single place that encodes/decodes `bytes` callback_data | skeleton done |
| `telegram/router.py` | Maps commands/messages to engine dispatch; owns the settings/quality-select menus | skeleton stub, real handlers land in M1/M2 |
| `db/models.py` | SQLAlchemy 2.0 models, byte-for-byte schema parity with the old bot | done, tested against expected column sets |
| `db/session.py` | Engine/session factory for the shared `DB_DSN` | done |
| `credits/service.py` | Ported quota/credit logic (free-then-paid deduction, 200MB/credit, bandwidth cap, owner bypass), with the pre-charge-before-split bug fixed | done, unit tested |
| `engines/base.py` | Shared engine contract (`matches()`, `download()`) | skeleton interface only; real base class (cache check → download → split → upload → archive → credit) lands in M2, ported from the old `engine/base.py` flow |
| `engines/{youtube,tiktok,instagram,direct}.py` | Per-platform engines | not started — M2/M3 |
| `queue/limiter.py` | Per-user + global concurrency caps via `asyncio.Semaphore` | skeleton done, wired up in M2 |
| `upload/splitter.py` | ffmpeg-based >2GB video splitting for Telegram's upload limit | skeleton stub (size check only); full split/relabel logic ported in M2 |
| `bootstrap.py` | Entrypoint wiring; only runs when invoked directly, never at import | done |

## 4. Environment the user must provide on the server

None of this is optional — without it, YouTube downloads fail in the new bot
exactly as they would in the old one, because both sit on top of the same
yt-dlp extraction pipeline. This is infrastructure the **user** sets up; it
is not something the rewrite can work around in code.

1. **A JavaScript runtime in `PATH`.** The old bot's README states this
   explicitly (`README.md:152`, `README-he.md:152`): Node.js must be
   installed and reachable, because yt-dlp needs to execute YouTube's
   signature/`n`-parameter challenge JS. `yt-dlp-ejs` (declared in
   `pyproject.toml`) still needs a JS engine to run against — confirm which
   runtime it expects (Node vs. Deno vs. a bundled engine) before M2, since
   yt-dlp's JS-runtime story has changed more than once; treat the exact
   requirement as something to verify against the yt-dlp-ejs version pinned
   at implementation time, not assumed from memory.
2. **`PATH` visibility under systemd.** A `systemd` service does not inherit
   the interactive shell's `PATH` by default. If Node/ffmpeg/etc. are
   installed via nvm, asdf, or a user-local prefix, the systemd unit's
   `ExecStart` will not find them unless `Environment=PATH=...` (or
   `EnvironmentFile=`) is set explicitly in the unit file. This is a known
   sharp edge and should be checked, not assumed, before M1 goes live.
3. **IPv4 preference.** YouTube's IPv6 ranges get rate-limited/blocked more
   aggressively than IPv4 on many hosts. If the server has IPv6 enabled and
   yt-dlp is not pinned to `--force-ipv4` (or the equivalent
   `source_address`/socket option), download failures can look like generic
   extraction errors instead of a network routing issue. Confirm the
   server's network posture before M2 and decide whether to force IPv4 by
   default.
4. **Cookies for age-restricted/private content.** `INSTAGRAM_COOKIES_FILE`,
   `INSTAGRAM_SESSION_FILE`, and `TIKTOK_COOKIES_FILE` (old bot's
   `config.py:72-79`) point at exported cookie files. These must exist on
   the server filesystem, in the right format (Netscape for yt-dlp,
   instaloader's own session format for Instagram), and stay refreshed —
   they expire. The new bot inherits the same requirement; there is no
   code-only substitute for logged-in cookies on IG/TikTok content that
   requires auth.
5. **YouTube PO token, if required.** `BROWSERS` and `POTOKEN`
   (`generic.py:832,837` in the old bot) exist because YouTube's bot
   detection increasingly requires a PO token or a real browser cookie jar
   to unlock certain formats/qualities. Whether this is currently required
   depends on YouTube's enforcement at deploy time — this needs to be
   re-verified against current yt-dlp guidance when M2 starts, not assumed
   from the old bot's setup, since PO token requirements have shifted over
   time.
6. **ffmpeg binary in `PATH`.** Required for probing, thumbnailing, and the
   >2GB video-split logic. Same requirement as the old bot
   (`README.md:151`).

None of items 1-6 were installed, tested, or verified live as part of this
stage (no network access to yt-dlp, no `--simulate` runs performed) — this
section is a checklist for the user to confirm/action before M2, not a
report of current server state.

## 5. Milestones (v1 scope)

Each milestone has a measurable acceptance bar, not just a feature list.
Performance targets are there so an improvement over the old bot is provable,
not just claimed.

### M1 — Skeleton, DB parity, bot answers `/start`
- Telethon client connects with the bot token, responds to `/start`,
  `/help`, `/about`, `/ping`, `/settings` (settings menu renders and toggles
  persist to the `settings` table).
- `users`/`settings`/`payments`/`video_cache` tables read/write correctly
  against the same DSN the old bot uses; schema-parity tests pass (already
  in the skeleton).
- Credit checks (`check_quota`, `use_quota_dynamic`, bandwidth cap, owner
  bypass) enforced identically to the old bot; `tests/test_credits_service.py`
  passes.
- **Acceptance:** a fresh clone with `uv sync && uv run pytest` is green with
  zero network/Telegram access; manual `/start` round-trip against a test
  bot token (never the production token) responds in under 1 second.

### M2 — YouTube + direct links end-to-end
- YouTube engine: quality-select menu, download, upload (including >2GB
  split), archive-channel forward, download cache, credit deduction after
  successful upload (pre-charge bug fixed, see `INVENTORY.md` §2).
- Direct-link engine (`/direct` or equivalent) for arbitrary HTTP(S) URLs.
- **Acceptance, functional:** 20 varied real YouTube URLs (different
  lengths, at least 2 over 2GB at 1080p to exercise the splitter) succeed
  end-to-end without manual intervention; at least 3 direct-link file types
  (video, archive, PDF or similar) succeed.
- **Acceptance, performance:** for a 10-minute 1080p YouTube video, median
  time from link received to file fully sent is **at or below the old bot's
  measured median** for the same test set (baseline to be measured against
  the running old bot before M2 sign-off — this spec does not assume the
  number, it commits to measuring and beating it). Download+upload success
  rate ≥ 95% across the 20-URL test set, excluding content genuinely
  unavailable (private/deleted/geo-blocked).

### M3 — TikTok + Instagram
- TikTok engine (including the `vm.tiktok.com` fix noted in
  `INVENTORY.md` §4) and Instagram engine (public content at minimum;
  cookie-gated content if the user has provided valid cookies per §4).
- **Acceptance:** 15 TikTok URLs (mix of `tiktok.com`, `vt.tiktok.com`,
  `vm.tiktok.com`, at least 2 slideshows) and 15 Instagram URLs (posts,
  reels, at least 2 carousels) succeed end-to-end. Success rate ≥ 90%
  (lower bar than YouTube because both platforms actively fight scrapers).

### M4 — Hardening and cutover readiness
- Concurrency limiter enforced under load (simulate ≥10 simultaneous
  downloads from different test users, confirm per-user/global caps hold
  and no crash/deadlock).
- Structured logs rotating correctly under sustained load; no unbounded
  memory growth in the in-process state module (`INVENTORY.md` §3) after a
  soak test of at least 500 sequential downloads.
- Full regression pass of M2+M3 acceptance sets after any dependency bumps.
- **Acceptance:** side-by-side run against the old bot (different bot token,
  same test account behavior) for one week of real usage patterns (or a
  synthetic equivalent), zero data corruption in the shared-schema tables,
  crash-free for the full soak window.

## 6. Cutover plan

The core constraint: same token, same DB, users must never notice a gap or
a credit discrepancy.

1. **Never run both bots against the production token simultaneously.**
   Telegram allows only one active `getUpdates`/MTProto session consumer per
   bot token in practice — running both risks duplicate replies, dropped
   updates, or Telegram-side session conflicts. The new bot is developed and
   tested end-to-end against a **separate test bot token** (and, ideally, a
   copy of the schema — not the live data — for early testing) through M1-M3.
2. **Read-only dry run against production data.** Once M4 sign-off is close,
   point the new bot's `DB_DSN` at the real production database but keep it
   on the test token, so credit/settings reads are validated against real
   user rows without any user-facing exposure.
3. **Freeze window.** Pick a low-traffic window. Stop the old bot's process
   (`systemctl stop <old-service>`) — this is the point where the token
   becomes free.
4. **Point the new bot at the production token and production `DB_DSN`.**
   Start it under its own systemd unit. Confirm `/start` and one real
   download succeed before declaring cutover complete.
5. **Keep the old bot's code and systemd unit in place but disabled** (not
   deleted) for a rollback window — if the new bot regresses, `systemctl
   stop media-bot-v2 && systemctl start <old-service>` should be a one-line
   rollback with zero data loss, since both bots share the same DB and never
   ran concurrently.
6. **Decommission the old bot's process only** (not its code/repo) once the
   new bot has run cleanly through the rollback window (suggest matching the
   M4 soak duration — at least one week — before removing the old systemd
   unit).

No DB migration step exists in this plan because none is needed — the
schema is identical by construction (§1 decision 2, enforced by
`tests/test_models_match_old_schema.py`).

## 7. Open questions for the user

1. **Payments (`/buy`, Stripe `PROVIDER_TOKEN`, `credit_account`).** Is
   purchasing additional credits in scope for v1, or is v1 "existing
   balances keep working, no new purchases" until a later milestone? This
   changes whether `PROVIDER_TOKEN`/`TOKEN_PRICE` need to be wired in M1-M4.
2. **Admin/ops minimum.** With the full admin panel out of v1 scope per the
   locked decisions, is *any* owner-only operational command needed for v1
   (e.g. manually crediting a user, blocking a bad actor, clearing the
   cache), or is direct DB access acceptable as the stopgap until a real
   admin panel is designed later?
3. **`about` text attribution.** The old bot's `/about` credits
   `@BennyThink` (upstream open-source project) and `@YD_IL` (operator).
   Keep, update, or remove this in the new bot?
4. **Resume-on-failure UX.** The old bot has a "resume download" button
   backed by in-process partial-download state (`INVENTORY.md` §4,
   `network_errors.py`). This is real complexity for a v1 rewrite — is a
   plain "download failed, send the link again" acceptable for v1, deferring
   true resume to a later milestone?
5. **Production `DB_DSN` dialect.** `requirements.txt` includes `PyMySQL`
   and the old model imports MySQL-specific `JSON`, strongly suggesting
   production is MySQL, not the SQLite file in the repo (which is an empty
   local dev copy). Please confirm the actual production dialect so M1's DB
   integration testing targets the right engine (`pymysql` is already in
   `pyproject.toml` on that assumption).
6. **GitHub repo timing.** `Omer-Dahan/media-bot-v2` is referenced as
   "will be created" — is it live yet? Until it exists, this stays a local
   git repo per the locked decision; let me know when to add the remote.
7. **PO token / cookie sourcing.** Who owns keeping YouTube/Instagram/TikTok
   cookies and any PO token fresh on the server long-term — is that a manual
   periodic task for the user, or should M2/M3 include tooling (e.g. an
   admin command or a scheduled check) to detect stale/expired cookies
   before they cause silent download failures?
