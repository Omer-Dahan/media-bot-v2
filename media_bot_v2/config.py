"""Typed configuration loaded from environment variables / .env.

Variable names are LOCKED to match the old bot (src/config/config.py) where
the same setting is reused, so the existing systemd unit / .env on the
server keeps working unchanged during cutover. New variables introduced by
this rewrite are documented in spec/SPEC.md.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The old bot's Telethon session file name. Starting this bot with the same
# session name would open/corrupt the old bot's live MTProto session while
# it's running in production against the same bot token.
OLD_BOT_SESSION_NAME = "main"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Telegram core (same token/app as the old bot; do not rotate) ---
    app_id: int = Field(validation_alias="APP_ID")
    app_hash: str = Field(validation_alias="APP_HASH")
    bot_token: str = Field(validation_alias="BOT_TOKEN")
    owner: str = Field(default="", validation_alias="OWNER")

    # Telethon session file name. MUST differ from the old bot's ("main"), or
    # starting this bot would corrupt/steal the old bot's live MTProto
    # session while it's running in production against the same bot token.
    session_name: str = Field(default="v2", validation_alias="SESSION_NAME")

    # Flood wait handling: seconds Telethon will automatically sleep internally
    # when Telegram returns FLOOD_WAIT / FLOOD_PREMIUM_WAIT. Kraken sets this to 0
    # so flood wait errors surface immediately to our custom retry/lane-reduction
    # logic rather than silently sleeping in Telethon for up to 60s. Default is 0
    # (Kraken parity), but configurable via FLOOD_SLEEP_THRESHOLD so it can be
    # reverted to Telethon's default (60) without code changes if ever desired.
    flood_sleep_threshold: int = Field(default=0, ge=0, validation_alias="FLOOD_SLEEP_THRESHOLD")

    # --- Database (same DSN/schema as the old bot) ---
    db_dsn: str = Field(default="sqlite:///database.sqlite3", validation_alias="DB_DSN")

    # --- Credits / VIP ---
    enable_vip: bool = Field(default=False, validation_alias="ENABLE_VIP")
    free_download: int = Field(default=3, validation_alias="FREE_DOWNLOAD")
    free_bandwidth: int = Field(default=2_147_483_648, validation_alias="FREE_BANDWIDTH")
    # One credit per this many MB delivered in a request (rounded up, min 1).
    # 200 matches the old bot.
    mb_per_credit: int = Field(default=200, gt=0, validation_alias="MB_PER_CREDIT")

    # --- Archive channel ---
    # `int | str | None`, not `str | None`: Telethon resolves a numeric-ish
    # string (any string of digits, optionally with a leading `-`, e.g. the
    # `-100...` supergroup/channel IDs Telegram uses) as a *phone number*
    # via `GetContactsRequest` - which bots are not allowed to call, so a
    # numeric channel ID passed as a plain string always fails with
    # "Cannot get entity by phone number as a bot". `_normalize_archive_channel`
    # below converts a digit string to `int` (which Telethon resolves as a
    # peer ID, correctly) and leaves an `@username` as `str`; anything else
    # is a configuration error raised at startup, not a silent failure
    # discovered later in an archive-copy warning log.
    archive_channel: int | str | None = Field(default=None, validation_alias="ARCHIVE_CHANNEL")

    @field_validator("archive_channel", mode="before")
    @classmethod
    def _normalize_archive_channel(cls, value: object) -> int | str | None:
        if value is None or isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("@"):
            return text
        digits = text.removeprefix("-")
        if digits.isdigit():
            return int(text)
        raise ValueError(
            f"ARCHIVE_CHANNEL={text!r} אינו בפורמט נתמך. "
            "השתמשו במזהה מספרי של ערוץ/סופרגרופ (למשל -1003534083142) "
            "או בשם משתמש של ערוץ ציבורי שמתחיל ב-@ (למשל @my_channel)."
        )

    # --- Local storage for in-flight downloads (deleted after each upload) ---
    download_dir: str = Field(default="downloads", validation_alias="DOWNLOAD_DIR")

    # --- Logging ---
    log_file: str = Field(default="logs/bot.log", validation_alias="LOG_FILE")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, validation_alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=5, validation_alias="LOG_BACKUP_COUNT")
    # Mirror logs to stdout so `journalctl -u download-bot-v2` shows them.
    log_to_console: bool = Field(default=True, validation_alias="LOG_TO_CONSOLE")

    # --- YouTube / Network options ---
    force_ipv4: bool = Field(default=False, validation_alias="FORCE_IPV4")
    potoken: str | None = Field(default=None, validation_alias="POTOKEN")
    # Base URL of a self-hosted PO token provider server (e.g. bgutil-ytdlp-pot-provider,
    # see docs/DEPLOY.md). Optional: only used by media_bot_v2.preflight to verify the
    # provider is reachable before cutover; the engine itself only consumes POTOKEN above.
    potoken_provider_url: str | None = Field(default=None, validation_alias="POTOKEN_PROVIDER_URL")
    youtube_cookies_file: str | None = Field(default=None, validation_alias="YOUTUBE_COOKIES_FILE")
    youtube_player_client: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YOUTUBE_PLAYER_CLIENT", "PLAYER_CLIENT"),
    )
    youtube_js_runtimes: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YOUTUBE_JS_RUNTIMES", "JS_RUNTIMES"),
    )
    youtube_remote_components: str | None = Field(
        default=None,
        validation_alias=AliasChoices("YOUTUBE_REMOTE_COMPONENTS", "REMOTE_COMPONENTS"),
    )

    # --- Providers & Extraction Layer (M3/M6) ---
    tiktok_cookies_file: str | None = Field(default=None, validation_alias="TIKTOK_COOKIES_FILE")
    instagram_cookies_file: str | None = Field(default=None, validation_alias="INSTAGRAM_COOKIES_FILE")
    tiktok_providers: str = Field(
        default="tikwm,tikdownloader,musicaldown,cobalt",
        validation_alias="TIKTOK_PROVIDERS",
    )
    youtube_providers: str = Field(
        default="ytmp3,cobalt",
        validation_alias="YOUTUBE_PROVIDERS",
    )
    disabled_providers: str = Field(
        default="",
        validation_alias="DISABLED_PROVIDERS",
    )
    cobalt_url: str | None = Field(default=None, validation_alias="COBALT_URL")
    # No default: this key belongs to the ytmp3.gl/gammacloud.net service,
    # not to this codebase - a committed fallback here is a leaked secret the
    # moment the repo is public. Unset means the provider is skipped
    # entirely (see build_provider_registry below), not attempted with an
    # empty key.
    ytmp3_api_key: str | None = Field(default=None, validation_alias="YTMP3_API_KEY")
    provider_timeout: float = Field(default=15.0, validation_alias="PROVIDER_TIMEOUT")
    provider_failure_threshold: int = Field(default=3, validation_alias="PROVIDER_FAILURE_THRESHOLD")
    provider_cooldown_seconds: int = Field(default=300, validation_alias="PROVIDER_COOLDOWN_SECONDS")

    # --- Concurrency limits ---
    # NOTE: workers/user_workers gate how many requests may be *in flight*
    # (asyncio.Semaphore only). The actual ceiling on blocking work inside a
    # request (download, mp3 conversion, ffprobe, split, upload part I/O) is
    # thread_pool_size below - see media_bot_v2/executor.py. Raising workers
    # without also raising thread_pool_size changes nothing.
    workers: int = Field(default=100, validation_alias="WORKERS")
    # 2 -> 5 (M11.11): 2 let a single active user queue behind their own
    # cap while WORKERS=100 sat mostly idle; 5 gives one user real headroom
    # without letting them monopolize the global pool.
    user_workers: int = Field(default=5, validation_alias="USER_WORKERS")
    # The dedicated thread pool's size - the real concurrency ceiling for
    # to_thread work (see media_bot_v2/executor.py's module docstring for
    # why Python's default executor, min(32, cpu+4), silently capped this
    # regardless of WORKERS/USER_WORKERS).
    thread_pool_size: int = Field(default=48, gt=0, validation_alias="THREAD_POOL_SIZE")
    # Account-health ceiling on concurrent *uploads* (not downloads), each of
    # which may open up to UPLOAD_CONNECTIONS real TCP connections to
    # Telegram - see media_bot_v2/telegram/parallel_upload.py's module
    # docstring for why this is bounded separately from WORKERS.
    upload_concurrency_limit: int = Field(default=20, gt=0, validation_alias="UPLOAD_CONCURRENCY_LIMIT")

    # --- Request timeout budget ---
    request_timeout: float = Field(default=600.0, validation_alias="REQUEST_TIMEOUT")
    upload_timeout: float = Field(default=600.0, validation_alias="UPLOAD_TIMEOUT")
    # Parallel upload lanes per file (1..5). Above 5 is clamped to 5; below 1 is
    # an error. More lanes upload faster but raise the FLOOD_WAIT risk.
    upload_workers: int = Field(default=5, validation_alias="UPLOAD_WORKERS")
    # Real TCP connections used by one upload (1..5, at most UPLOAD_WORKERS).
    # 1 = every lane shares the main connection (M10 behaviour).
    upload_connections: int = Field(default=5, validation_alias="UPLOAD_CONNECTIONS")
    # Budget for making a video Telegram-playable (ffmpeg). Separate from, and
    # smaller than, the upload budget; on expiry the original file is sent.
    convert_timeout: float = Field(default=180.0, validation_alias="CONVERT_TIMEOUT")

    # --- Download limits (not user-configurable, kept as constants for clarity) ---
    tg_normal_max_size: int = 2000 * 1024 * 1024
    max_download_size: int = 4 * 1024 * 1024 * 1024

    @field_validator("upload_workers")
    @classmethod
    def _clamp_upload_workers(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"UPLOAD_WORKERS must be between 1 and 5 (got {value})")
        return min(value, 5)

    @field_validator("upload_connections")
    @classmethod
    def _clamp_upload_connections(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"UPLOAD_CONNECTIONS must be between 1 and 5 (got {value})")
        return min(value, 5)

    @field_validator("session_name")
    @classmethod
    def _forbid_old_bot_session_name(cls, value: str) -> str:
        if value == OLD_BOT_SESSION_NAME:
            raise ValueError(
                f"SESSION_NAME={OLD_BOT_SESSION_NAME!r} is the old bot's live session file. "
                "Starting this bot with it would corrupt that session while the old bot is "
                "running in production. Set SESSION_NAME to something else (default 'v2')."
            )
        return value

    @property
    def owner_ids(self) -> list[int]:
        return [int(i.strip()) for i in self.owner.split(",") if i.strip().isdigit()]

    @property
    def parsed_tiktok_providers(self) -> list[str]:
        return [p.strip().lower() for p in self.tiktok_providers.split(",") if p.strip()]

    @property
    def parsed_youtube_providers(self) -> list[str]:
        return [p.strip().lower() for p in self.youtube_providers.split(",") if p.strip()]

    @property
    def parsed_disabled_providers(self) -> set[str]:
        return {p.strip().lower() for p in self.disabled_providers.split(",") if p.strip()}



def load_settings() -> Settings:
    return Settings()
