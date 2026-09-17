"""Application entrypoint wiring: config -> logging -> DB -> Telethon client.

Importing this module does nothing. Only calling main() starts the bot, and
main() is only invoked from the `if __name__ == "__main__":` guard below -
never at import time, so this file is safe to import from tests.
"""

from __future__ import annotations

import logging

from media_bot_v2.config import load_settings
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.session import build_session_factory
from media_bot_v2.logging_setup import configure_logging
from media_bot_v2.telegram.client import build_client
from media_bot_v2.telegram.router import register_handlers

logger = logging.getLogger(__name__)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_file, settings.log_max_bytes, settings.log_backup_count)

    session_factory = build_session_factory(settings.db_dsn)
    CreditsService(
        session_factory,
        enable_vip=settings.enable_vip,
        owner_ids=settings.owner_ids,
        free_bandwidth=settings.free_bandwidth,
    )

    client = build_client(settings)
    register_handlers(client)

    logger.info("Starting media-bot-v2")
    client.start(bot_token=settings.bot_token)
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
