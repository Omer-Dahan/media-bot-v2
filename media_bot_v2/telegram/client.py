"""Telethon client bootstrap.

Building the client here does not connect to Telegram - call `.start()` (or
`.run_until_disconnected()`) only from the real entrypoint, never at import
time or in tests.
"""

from __future__ import annotations

from telethon import TelegramClient

from media_bot_v2.config import Settings


def build_client(settings: Settings, *, session_name: str = "media_bot_v2") -> TelegramClient:
    return TelegramClient(
        session_name,
        settings.app_id,
        settings.app_hash,
        flood_sleep_threshold=settings.flood_sleep_threshold,
    )
