"""Application entrypoint wiring: config -> logging -> DB -> Telethon client.

Importing this module does nothing. Only calling main() starts the bot, and
main() is only invoked from the `if __name__ == "__main__":` guard below -
never at import time, so this file is safe to import from tests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from telethon import TelegramClient

from media_bot_v2.cache.video_cache import VideoCacheStore
from media_bot_v2.config import load_settings
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.session import build_session_factory
from media_bot_v2.engines.youtube import check_js_runtime
from media_bot_v2.logging_setup import configure_logging
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.registry import build_provider_registry
from media_bot_v2.queue.limiter import ConcurrencyLimiter
from media_bot_v2.telegram.client import build_client
from media_bot_v2.telegram.flood_wait import FLOOD_WAIT_ERRORS, get_flood_wait_seconds
from media_bot_v2.telegram.router import register_handlers

logger = logging.getLogger(__name__)


def start_client_with_flood_retry(
    client: TelegramClient,
    *,
    bot_token: str,
    max_retries: int = 3,
    max_wait_seconds: float = 60.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Start the Telethon client, retrying gracefully if Telegram returns a flood wait on startup."""
    attempts = 0
    while True:
        try:
            client.start(bot_token=bot_token)
            return
        except FLOOD_WAIT_ERRORS as exc:
            attempts += 1
            wait_seconds = get_flood_wait_seconds(exc)
            if attempts > max_retries or wait_seconds > max_wait_seconds:
                logger.error(
                    "Telegram flood wait on startup (%s: %ss, attempt %d/%d) exceeded limits; aborting",
                    type(exc).__name__,
                    wait_seconds,
                    attempts,
                    max_retries,
                )
                raise
            logger.warning(
                "Telegram flood wait on startup (%s: %ss, attempt %d/%d); sleeping %ss before retry",
                type(exc).__name__,
                wait_seconds,
                attempts,
                max_retries,
                wait_seconds,
            )
            sleeper(wait_seconds)


def main() -> None:
    settings = load_settings()
    configure_logging(
        settings.log_file,
        settings.log_max_bytes,
        settings.log_backup_count,
        log_to_console=settings.log_to_console,
    )
    check_js_runtime()

    session_factory = build_session_factory(settings.db_dsn)
    credits_service = CreditsService(
        session_factory,
        enable_vip=settings.enable_vip,
        owner_ids=settings.owner_ids,
        free_bandwidth=settings.free_bandwidth,
        mb_per_credit=settings.mb_per_credit,
    )
    pipeline = DownloadPipeline(
        credits_service=credits_service,
        download_dir=Path(settings.download_dir),
        request_timeout=settings.request_timeout,
        download_timeout=settings.request_timeout,
        upload_timeout=settings.upload_timeout,
        convert_timeout=settings.convert_timeout,
    )
    limiter = ConcurrencyLimiter(
        global_limit=settings.workers,
        per_user_limit=settings.user_workers,
    )
    video_cache_store = VideoCacheStore(session_factory)
    health_tracker = ProviderHealthTracker(
        session_factory,
        failure_threshold=settings.provider_failure_threshold,
        cooldown_seconds=settings.provider_cooldown_seconds,
    )
    registry = build_provider_registry(settings)

    client = build_client(settings, session_name=settings.session_name)
    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=settings.free_download,
        pipeline=pipeline,
        archive_channel=settings.archive_channel,
        upload_workers=settings.upload_workers,
        upload_connections=settings.upload_connections,
        max_download_size=settings.max_download_size,
        limiter=limiter,
        force_ipv4=settings.force_ipv4,
        youtube_cookies_file=settings.youtube_cookies_file,
        potoken=settings.potoken,
        video_cache_store=video_cache_store,
        youtube_player_client=settings.youtube_player_client,
        youtube_js_runtimes=settings.youtube_js_runtimes,
        youtube_remote_components=settings.youtube_remote_components,
        health_tracker=health_tracker,
        registry=registry,
        tiktok_cookies_file=settings.tiktok_cookies_file,
        instagram_cookies_file=settings.instagram_cookies_file,
    )

    logger.info("Starting media-bot-v2")
    start_client_with_flood_retry(client, bot_token=settings.bot_token)
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
