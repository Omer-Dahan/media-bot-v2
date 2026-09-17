"""Router wiring smoke test: register_handlers must attach every handler
without connecting to Telegram. Uses telethon's in-memory session, same
"never touches the network or disk" spirit as test_telegram_bootstrap.py."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
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
        max_download_size=4 * 1024 * 1024 * 1024,
    )

    handlers = client.list_event_handlers()
    assert len(handlers) >= 8  # /start /help /about /ping /settings toggle ytq url_handler


def _find_url_handler(client: TelegramClient):
    for callback, event in client.list_event_handlers():
        if isinstance(event, events.NewMessage) and event.pattern is None:
            return callback
    raise AssertionError("url_handler (bare NewMessage handler) not registered")


class _FakeSender:
    first_name = "Test User"
    username = "testuser"


class _FakeMessage:
    async def edit(self, *args, **kwargs) -> None:
        pass


class _FakeEvent:
    def __init__(self, text: str, sender_id: int):
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.sender = _FakeSender()

    async def respond(self, *args, **kwargs):
        return _FakeMessage()


async def test_url_handler_creates_user_before_running_pipeline():
    """Covers finding 1: a user who sends a direct link without ever
    running /start must still get a DB row (and therefore be subject to
    quota enforcement) - previously url_handler ran the pipeline directly
    and check_quota silently no-op'd for a user row that didn't exist yet."""
    client = TelegramClient(MemorySession(), 1, "hash")

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    # free_download=0 so the freshly created user has zero credits: if
    # get_or_create_user ran, check_quota raises immediately (before ever
    # touching the network) instead of silently allowing the download.
    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=1)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=Path("/tmp/media-bot-v2-test"))

    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=0,
        pipeline=pipeline,
        archive_channel=None,
        max_download_size=4 * 1024 * 1024 * 1024,
    )

    url_handler = _find_url_handler(client)
    new_user_id = 424242
    await url_handler(_FakeEvent("http://example.local/some-file.bin", new_user_id))

    with session_factory() as session:
        user = session.query(User).filter(User.user_id == new_user_id).one()
        assert user.free == 0
        assert user.paid == 0
