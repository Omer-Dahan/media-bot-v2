"""Router wiring smoke test: register_handlers must attach every handler
without connecting to Telegram. Uses telethon's in-memory session, same
"never touches the network or disk" spirit as test_telegram_bootstrap.py."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from media_bot_v2.cache.video_cache import compute_cache_key
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.tiktok import TikTokEngine
from media_bot_v2.engines.youtube import YouTubeDownloadError, YouTubeEngine
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.queue.limiter import ConcurrencyLimiter
from media_bot_v2.telegram import settings_menu, texts
from media_bot_v2.telegram.callback_data import encode
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
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit(self, text: str = "", *args, **kwargs) -> None:
        self.edits.append(text)


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


def _find_toggle_handler(client: TelegramClient):
    for callback, event in client.list_event_handlers():
        if (
            isinstance(event, events.CallbackQuery)
            and getattr(event, "match", None)
            and event.match(b"toggle_quality")
        ):
            return callback
    raise AssertionError("toggle_handler (CallbackQuery ^toggle_) not registered")


def _find_ytq_handler(client: TelegramClient):
    for callback, event in client.list_event_handlers():
        if (
            isinstance(event, events.CallbackQuery)
            and getattr(event, "match", None)
            and event.match(b"ytq:720:abc")
        ):
            return callback
    raise AssertionError("ytq_handler (CallbackQuery ^ytq:) not registered")


class _FakeCallbackEvent:
    def __init__(self, data: bytes, sender_id: int):
        self.data = data
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.answer_calls: list[tuple[str | None, bool]] = []
        self.messages: list[_FakeMessage] = []

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answer_calls.append((text, alert))

    async def edit(self, *args, **kwargs):
        return None

    async def respond(self, text: str, *args, **kwargs) -> _FakeMessage:
        msg = _FakeMessage()
        self.messages.append(msg)
        return msg


def _make_router(
    session_factory=None,
    pipeline=None,
    limiter=None,
    archive_channel=None,
    enable_vip=True,
    owner_ids=None,
    free_download=3,
    credits_service=None,
):
    client = TelegramClient(MemorySession(), 1, "hash")
    if session_factory is None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
    if credits_service is None:
        credits_service = CreditsService(
            session_factory,
            enable_vip=enable_vip,
            owner_ids=owner_ids or [],
            free_bandwidth=1,
        )
    if pipeline is None:
        pipeline = DownloadPipeline(credits_service=credits_service, download_dir=Path("/tmp/media-bot-v2-test"))
    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=free_download,
        pipeline=pipeline,
        archive_channel=archive_channel,
        max_download_size=4 * 1024 * 1024 * 1024,
        limiter=limiter,
    )
    return client


async def test_toggle_title_length_answers_with_alert_true():
    """Covers the M1.1 follow-up: the title-length toggle's explanatory
    answer text is easy to miss as a toast, so it must be popped as an
    alert dialog - assert the actual `alert=True` kwarg reaches event.answer,
    not just that some answer text is sent."""
    client = _make_router()
    toggle_handler = _find_toggle_handler(client)

    event = _FakeCallbackEvent(encode(settings_menu.TOGGLE_TITLE_LEN), sender_id=1)
    await toggle_handler(event)

    assert len(event.answer_calls) == 1
    _, alert = event.answer_calls[0]
    assert alert is True


async def test_toggle_quality_answers_without_alert():
    """A plain value-flip toggle (quality/format/subtitles) should stay a
    quiet toast, not an alert dialog - only the title-length toggle explains
    a behavior change large enough to warrant interrupting the user."""
    client = _make_router()
    toggle_handler = _find_toggle_handler(client)

    event = _FakeCallbackEvent(encode(settings_menu.TOGGLE_QUALITY), sender_id=1)
    await toggle_handler(event)

    assert len(event.answer_calls) == 1
    _, alert = event.answer_calls[0]
    assert alert is False


async def test_quality_pick_answers_alert_when_link_expired():
    client = _make_router()
    ytq_handler = _find_ytq_handler(client)

    event = _FakeCallbackEvent(encode("ytq", "720", "deadbeef"), sender_id=1)
    await ytq_handler(event)

    assert len(event.answer_calls) == 1
    assert event.answer_calls[0] == (texts.YOUTUBE_LINK_EXPIRED, True)


class _QualityEvent(_FakeEvent):
    def __init__(self, text: str, sender_id: int):
        super().__init__(text, sender_id)
        self.buttons = []

    async def respond(self, text, *args, buttons=None, **kwargs):
        self.buttons = buttons
        return _FakeMessage()


async def test_quality_pick_valid_link_runs_pipeline_with_youtube_engine():
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, archive_channel="@my_archive")
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    await url_handler(url_event)

    assert url_event.buttons
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    await ytq_handler(cb_event)

    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 1
    assert kwargs["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert isinstance(kwargs["engine"], YouTubeEngine)
    assert kwargs["engine"]._quality == "1080"
    assert kwargs["engine"]._is_playlist is False
    assert kwargs["engine"]._playlist_item_limit is None
    assert kwargs["cache_key"] == compute_cache_key("dQw4w9WgXcQ", "1080")
    assert kwargs["archive_channel"] == "@my_archive"


async def test_quality_pick_shows_queue_wait_when_limiter_busy():
    limiter = ConcurrencyLimiter(global_limit=1, per_user_limit=1)
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, limiter=limiter)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    # Hold the slot so the next caller must wait
    async with limiter.slot(user_id=2):
        cb_event = _FakeCallbackEvent(button_data, sender_id=1)
        task = asyncio.create_task(ytq_handler(cb_event))
        await asyncio.sleep(0.02)
        assert len(cb_event.messages) == 1
        assert texts.YOUTUBE_QUEUE_WAIT in cb_event.messages[0].edits
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_quality_pick_surfaces_classified_youtube_error():
    pipeline = AsyncMock()
    pipeline.run.side_effect = YouTubeDownloadError("הסרטון אינו זמין (סרטון פרטי)")
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    await ytq_handler(cb_event)

    assert len(cb_event.messages) == 1
    assert "הסרטון אינו זמין (סרטון פרטי)" in cb_event.messages[0].edits


async def test_quality_pick_playlist_with_vip_disabled_downloads_full_playlist():
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, enable_vip=False)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/playlist?list=PLtest", 1)
    await url_handler(url_event)

    assert url_event.buttons
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    await ytq_handler(cb_event)

    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 1
    assert kwargs["url"] == "https://www.youtube.com/playlist?list=PLtest"
    assert isinstance(kwargs["engine"], YouTubeEngine)
    assert kwargs["engine"]._is_playlist is True
    assert kwargs["engine"]._playlist_item_limit is None


async def test_quality_pick_playlist_with_credits_sets_limit():
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, enable_vip=True, free_download=5)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/playlist?list=PLtest", 1)
    await url_handler(url_event)

    assert url_event.buttons
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    await ytq_handler(cb_event)

    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 1
    assert kwargs["url"] == "https://www.youtube.com/playlist?list=PLtest"
    assert isinstance(kwargs["engine"], YouTubeEngine)
    assert kwargs["engine"]._is_playlist is True
    assert kwargs["engine"]._playlist_item_limit == 5


async def test_quality_pick_playlist_owner_with_zero_credits_downloads_unlimited():
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, enable_vip=True, owner_ids=[777], free_download=0)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/playlist?list=PLtest", 777)
    await url_handler(url_event)

    assert url_event.buttons
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=777)
    await ytq_handler(cb_event)

    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 777
    assert kwargs["url"] == "https://www.youtube.com/playlist?list=PLtest"
    assert isinstance(kwargs["engine"], YouTubeEngine)
    assert kwargs["engine"]._is_playlist is True
    assert kwargs["engine"]._playlist_item_limit is None


async def test_quality_pick_playlist_regular_user_zero_credits_shows_error():
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, enable_vip=True, owner_ids=[999], free_download=0)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/playlist?list=PLtest", 1)
    await url_handler(url_event)

    assert url_event.buttons
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    await ytq_handler(cb_event)

    pipeline.run.assert_not_called()
    assert len(cb_event.messages) == 1
    assert "הקרדיטים שלך נגמרו." in cb_event.messages[0].edits


async def test_url_handler_tiktok_link_runs_pipeline_with_tiktok_engine():
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline, archive_channel="@my_archive")
    url_handler = _find_url_handler(client)

    url_event = _FakeEvent("https://www.tiktok.com/@user/video/7123456789", 1)
    await url_handler(url_event)

    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 1
    assert kwargs["url"] == "https://www.tiktok.com/@user/video/7123456789"
    assert isinstance(kwargs["engine"], TikTokEngine)
    assert kwargs["archive_channel"] == "@my_archive"
    assert kwargs["cache_key"] == compute_cache_key("https://www.tiktok.com/@user/video/7123456789", "tiktok")


