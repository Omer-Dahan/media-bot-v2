"""Application entrypoint wiring: config -> logging -> DB -> Telethon client.

Importing this module does nothing. Only calling main() starts the bot, and
main() is only invoked from the `if __name__ == "__main__":` guard below -
never at import time, so this file is safe to import from tests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from media_bot_v2.config import load_settings
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.session import build_session_factory
from media_bot_v2.logging_setup import configure_logging
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram.client import build_client
from media_bot_v2.telegram.router import register_handlers

logger = logging.getLogger(__name__)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_file, settings.log_max_bytes, settings.log_backup_count)

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
    )

    client = build_client(settings, session_name=settings.session_name)
    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=settings.free_download,
        pipeline=pipeline,
        archive_channel=settings.archive_channel,
        max_download_size=settings.max_download_size,
    )

    logger.info("Starting media-bot-v2")
    client.start(bot_token=settings.bot_token)
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
