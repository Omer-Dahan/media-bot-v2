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
    archive_channel: str | None = Field(default=None, validation_alias="ARCHIVE_CHANNEL")

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
    ytmp3_api_key: str = Field(
        default="9b0ed5dab31616027ad7154140b0272d",
        validation_alias="YTMP3_API_KEY",
    )
    provider_timeout: float = Field(default=15.0, validation_alias="PROVIDER_TIMEOUT")
    provider_failure_threshold: int = Field(default=3, validation_alias="PROVIDER_FAILURE_THRESHOLD")
    provider_cooldown_seconds: int = Field(default=300, validation_alias="PROVIDER_COOLDOWN_SECONDS")

    # --- Concurrency limits ---
    workers: int = Field(default=100, validation_alias="WORKERS")
    user_workers: int = Field(default=2, validation_alias="USER_WORKERS")

    # --- Request timeout budget ---
    request_timeout: float = Field(default=600.0, validation_alias="REQUEST_TIMEOUT")
    upload_timeout: float = Field(default=600.0, validation_alias="UPLOAD_TIMEOUT")
    # Budget for making a video Telegram-playable (ffmpeg). Separate from, and
    # smaller than, the upload budget; on expiry the original file is sent.
    convert_timeout: float = Field(default=180.0, validation_alias="CONVERT_TIMEOUT")

    # --- Download limits (not user-configurable, kept as constants for clarity) ---
    tg_normal_max_size: int = 2000 * 1024 * 1024
    max_download_size: int = 4 * 1024 * 1024 * 1024

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
