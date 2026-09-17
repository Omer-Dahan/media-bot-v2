"""Router wiring smoke test: register_handlers must attach every handler
without connecting to Telegram. Uses telethon's in-memory session, same
"never touches the network or disk" spirit as test_telegram_bootstrap.py."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient
from telethon.sessions import MemorySession

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram.router import register_handlers


def test_register_handlers_attaches_handlers_without_connecting():
    client = TelegramClient(MemorySession(), 1, "hash")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=1)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=Path("/tmp/media-bot-v2-test"))

    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=3,
        pipeline=pipeline,
        archive_channel=None,
    )

    handlers = client.list_event_handlers()
    assert len(handlers) >= 8  # /start /help /about /ping /settings toggle ytq url_handler
