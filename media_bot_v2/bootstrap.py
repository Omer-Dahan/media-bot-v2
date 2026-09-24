"""Application entrypoint wiring: config -> logging -> DB -> Telethon client.

Importing this module does nothing. Only calling main() starts the bot, and
main() is only invoked from the `if __name__ == "__main__":` guard below -
never at import time, so this file is safe to import from tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

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
from media_bot_v2.telegram.router import register_handlers

logger = logging.getLogger(__name__)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_file, settings.log_max_bytes, settings.log_backup_count)
    check_js_runtime()

    session_factory = build_session_factory(settings.db_dsn)
    credits_service = CreditsService(
        session_factory,
        enable_vip=settings.enable_vip,
        owner_ids=settings.owner_ids,
        free_bandwidth=settings.free_bandwidth,
    )
    pipeline = DownloadPipeline(
        credits_service=credits_service,
        download_dir=Path(settings.download_dir),
        request_timeout=settings.request_timeout,
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
    client.start(bot_token=settings.bot_token)
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
