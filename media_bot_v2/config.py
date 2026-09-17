"""Typed configuration loaded from environment variables / .env.

Variable names are LOCKED to match the old bot (src/config/config.py) where
the same setting is reused, so the existing systemd unit / .env on the
server keeps working unchanged during cutover. New variables introduced by
this rewrite are documented in spec/SPEC.md.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Telegram core (same token/app as the old bot; do not rotate) ---
    app_id: int = Field(validation_alias="APP_ID")
    app_hash: str = Field(validation_alias="APP_HASH")
    bot_token: str = Field(validation_alias="BOT_TOKEN")
    owner: str = Field(default="", validation_alias="OWNER")

    # --- Database (same DSN/schema as the old bot) ---
    db_dsn: str = Field(default="sqlite:///database.sqlite3", validation_alias="DB_DSN")

    # --- Credits / VIP ---
    enable_vip: bool = Field(default=False, validation_alias="ENABLE_VIP")
    free_download: int = Field(default=3, validation_alias="FREE_DOWNLOAD")
    free_bandwidth: int = Field(default=2_147_483_648, validation_alias="FREE_BANDWIDTH")

    # --- Archive channel ---
    archive_channel: str | None = Field(default=None, validation_alias="ARCHIVE_CHANNEL")

    # --- Logging ---
    log_file: str = Field(default="logs/bot.log", validation_alias="LOG_FILE")
    log_max_bytes: int = Field(default=10 * 1024 * 1024, validation_alias="LOG_MAX_BYTES")
    log_backup_count: int = Field(default=5, validation_alias="LOG_BACKUP_COUNT")

    # --- Download limits (not user-configurable, kept as constants for clarity) ---
    tg_normal_max_size: int = 2000 * 1024 * 1024
    max_download_size: int = 4 * 1024 * 1024 * 1024

    @property
    def owner_ids(self) -> list[int]:
        return [int(i.strip()) for i in self.owner.split(",") if i.strip().isdigit()]


def load_settings() -> Settings:
    return Settings()
