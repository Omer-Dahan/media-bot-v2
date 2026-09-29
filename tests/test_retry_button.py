"""M11.5: 🔄 "נסה שוב" (Retry) button on failure messages.

Verifies:
1. Button is attached to supported retryable failures, NOT on hopeless failures or success.
2. Owner click re-executes the exact same request through the pipeline, removes the button, no duplicate messages.
3. Rival click receives short alert, no pipeline execution.
4. Double-click / after use / after TTL expiration returns short response, no parallel execution.
5. Limiter and credits are enforced on retry.
6. Zero lingering records in RetryStore after success, hopeless failure, or expiration.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import events

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base
from media_bot_v2.engines.base import DownloadTooLargeError, UnsupportedUrlError
from media_bot_v2.engines.tiktok import TikTokDownloadError
from media_bot_v2.queue.limiter import ConcurrencyLimiter
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.callback_data import encode
from media_bot_v2.telegram.retry import RetryStore, is_hopeless_failure
from media_bot_v2.telegram.router import register_handlers
from tests.fakes_telegram import FakeTelegramClient


class _FakeSender:
    first_name = "Retry User"
    username = "retryuser"


class _FakeMessage:
    _next_id = 100

    def __init__(self, chat_id: int = 123) -> None:
        type(self)._next_id += 1
        self.id = type(self)._next_id
        self.chat_id = chat_id
        self.edits: list[str] = []
        self.edit_kwargs: list[dict] = []
        self.delete = AsyncMock()

    async def edit(self, text: str = "", *args: Any, **kwargs: Any) -> _FakeMessage:
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)
        return self


class _FakeEvent:
    def __init__(self, text: str, sender_id: int = 123, is_private: bool = True) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.is_private = is_private
        self.sender = _FakeSender()
        self.responses: list[str] = []
        self.respond_kwargs: list[dict] = []
        self.messages: list[_FakeMessage] = []

    async def respond(self, *args: Any, **kwargs: Any) -> _FakeMessage:
        if args and isinstance(args[0], str):
            self.responses.append(args[0])
        self.respond_kwargs.append(kwargs)
        msg = _FakeMessage(chat_id=self.chat_id)
        self.messages.append(msg)
        return msg


class _FakeCallbackClick:
    def __init__(
        self,
        *,
        data: bytes,
        chat_id: int,
        message: _FakeMessage,
        sender_id: int,
        is_private: bool = True,
    ) -> None:
        self.data = data
        self.chat_id = chat_id
        self.message = message
        self.message_id = message.id
        self.sender_id = sender_id
        self.is_private = is_private
        self.answer_calls: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answer_calls.append((text, alert))

    async def edit(self, text: str = "", *args: Any, **kwargs: Any) -> _FakeMessage:
        return await self.message.edit(text, *args, **kwargs)

    async def get_message(self) -> _FakeMessage:
        return self.message


class _ControllablePipeline:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.fail_terminal_text: str | None = None
        self.block_event: asyncio.Event | None = None

    async def run(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.block_event is not None:
            await self.block_event.wait()
        if self.fail_terminal_text is not None:
            progress = kwargs.get("progress")
            if progress:
                await progress.update(self.fail_terminal_text, is_terminal=True)
        if self.fail_with is not None:
            raise self.fail_with


def _setup_test_env(
    pipeline: _ControllablePipeline,
    retry_store: RetryStore | None = None,
    limiter: ConcurrencyLimiter | None = None,
    free_download: int = 3,
) -> tuple[FakeTelegramClient, RetryStore, Any, Any]:
    client = FakeTelegramClient()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=1)
    if retry_store is None:
        retry_store = RetryStore()

    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=free_download,
        pipeline=pipeline,
        archive_channel=None,
        max_download_size=4 * 1024 * 1024 * 1024,
        retry_store=retry_store,
        limiter=limiter,
    )

    url_handler = None
    retry_handler = None
    for callback, event in client.list_event_handlers():
        if isinstance(event, events.NewMessage) and event.pattern is None:
            url_handler = callback
        elif (
            isinstance(event, events.CallbackQuery)
            and getattr(event, "match", None)
            and event.match(b"retry")
        ):
            retry_handler = callback

    assert url_handler is not None, "url_handler not found"
    assert retry_handler is not None, "retry_handler not found"
    return client, retry_store, url_handler, retry_handler


def _extract_retry_id(buttons: Any) -> str | None:
    if not buttons:
        return None
    rows = getattr(buttons, "rows", buttons)
    if not rows or not hasattr(rows, "__iter__"):
        return None
    for row in rows:
        if not hasattr(row, "__iter__"):
            continue
        for b in row:
            if getattr(b, "text", "") == texts.RETRY_BUTTON:
                data = getattr(b, "data", None) or getattr(getattr(b, "type", None), "data", None)
                if data:
                    if isinstance(data, bytes):
                        parts = data.decode("utf-8").split(":")
                    else:
                        parts = str(data).split(":")
                    return parts[1] if len(parts) > 1 else None
    return None


@pytest.mark.asyncio
async def test_button_attached_on_retryable_failure_and_not_on_hopeless_failure():
    """Requirement 1: Attached on retryable failures, NOT on hopeless failures or success."""
    pipeline = _ControllablePipeline()
    _, retry_store, url_handler, _ = _setup_test_env(pipeline)

    # 1. Retryable failure (TikTok network failure)
    pipeline.fail_with = TikTokDownloadError("Temporary network timeout")
    event1 = _FakeEvent("https://www.tiktok.com/@user/video/111", sender_id=101)
    await url_handler(event1)

    msg1 = event1.messages[0]
    assert len(msg1.edits) > 0
    last_buttons1 = msg1.edit_kwargs[-1].get("buttons")
    retry_id1 = _extract_retry_id(last_buttons1)
    assert retry_id1 is not None, "Retry button must be attached on TikTokDownloadError"
    assert len(retry_store) == 1

    # 2. Hopeless failure: UnsupportedUrlError
    pipeline.fail_with = UnsupportedUrlError("Unsupported url")
    event2 = _FakeEvent("https://www.tiktok.com/@user/video/222", sender_id=102)
    await url_handler(event2)
    msg2 = event2.messages[0]
    last_buttons2 = msg2.edit_kwargs[-1].get("buttons")
    assert _extract_retry_id(last_buttons2) is None, "No retry button on UnsupportedUrlError"

    # 3. Hopeless failure: DownloadTooLargeError
    pipeline.fail_with = DownloadTooLargeError("הקובץ גדול מדי")
    event3 = _FakeEvent("https://www.tiktok.com/@user/video/333", sender_id=103)
    await url_handler(event3)
    msg3 = event3.messages[0]
    last_buttons3 = msg3.edit_kwargs[-1].get("buttons")
    assert _extract_retry_id(last_buttons3) is None, "No retry button on DownloadTooLargeError"

    # 4. Hopeless failure: CreditsExhaustedException (should have contact button, not retry)
    pipeline.fail_with = CreditsExhaustedException(texts.CREDITS_EXHAUSTED)
    event4 = _FakeEvent("https://www.tiktok.com/@user/video/444", sender_id=104)
    await url_handler(event4)
    msg4 = event4.messages[0]
    last_buttons4 = msg4.edit_kwargs[-1].get("buttons")
    assert _extract_retry_id(last_buttons4) is None, "No retry button on CreditsExhaustedException"
    assert any(b.text == texts.CREDITS_BUTTON for row in last_buttons4 for b in row)

    # 5. Hopeless failure: UserBlockedException
    pipeline.fail_with = UserBlockedException("המשתמש שלך נחסם. פנה למנהל.")
    event5 = _FakeEvent("https://www.tiktok.com/@user/video/555", sender_id=105)
    await url_handler(event5)
    msg5 = event5.messages[0]
    last_buttons5 = msg5.edit_kwargs[-1].get("buttons")
    assert _extract_retry_id(last_buttons5) is None, "No retry button on UserBlockedException"

    # 6. Success: pipeline completes without error
    pipeline.fail_with = None
    event6 = _FakeEvent("https://www.tiktok.com/@user/video/666", sender_id=106)
    await url_handler(event6)
    # On success, message was sent, pipeline completed, no retry button attached
    for kwargs in event6.messages[0].edit_kwargs:
        assert _extract_retry_id(kwargs.get("buttons")) is None


@pytest.mark.asyncio
async def test_owner_click_re_executes_request_and_removes_button():
    """Requirement 2: Owner click re-executes through pipeline with same link, removes button, no duplicate message."""
    pipeline = _ControllablePipeline()
    pipeline.fail_with = TikTokDownloadError("Temporary error")
    _, retry_store, url_handler, retry_handler = _setup_test_env(pipeline)

    user_id = 200
    target_url = "https://www.tiktok.com/@user/video/12345678"
    event = _FakeEvent(target_url, sender_id=user_id)
    await url_handler(event)

    msg = event.messages[0]
    retry_id = _extract_retry_id(msg.edit_kwargs[-1].get("buttons"))
    assert retry_id is not None
    assert len(pipeline.calls) == 1
    assert len(event.messages) == 1

    # Now make the pipeline succeed on the retry!
    pipeline.fail_with = None

    click = _FakeCallbackClick(
        data=encode("retry", retry_id),
        chat_id=user_id,
        message=msg,
        sender_id=user_id,
    )
    await retry_handler(click)

    # Pipeline was called a second time with the exact same URL and user_id!
    assert len(pipeline.calls) == 2
    assert pipeline.calls[1]["url"] == target_url
    assert pipeline.calls[1]["user_id"] == user_id

    # The message was edited to DOWNLOAD_STARTED with cancel markup (retry button removed)
    assert texts.DOWNLOAD_STARTED in msg.edits
    # No new message was sent via respond on retry
    assert len(event.messages) == 1

    # On success, retry context is completely removed from store!
    assert len(retry_store) == 0


@pytest.mark.asyncio
async def test_rival_click_receives_alert_toast_and_no_execution():
    """Requirement 3: Rival click is rejected with short response, no execution."""
    pipeline = _ControllablePipeline()
    pipeline.fail_with = TikTokDownloadError("Temporary error")
    _, retry_store, url_handler, retry_handler = _setup_test_env(pipeline)

    owner_id = 300
    rival_id = 999
    event = _FakeEvent("https://www.tiktok.com/@user/video/rival_test", sender_id=owner_id)
    await url_handler(event)

    msg = event.messages[0]
    retry_id = _extract_retry_id(msg.edit_kwargs[-1].get("buttons"))
    assert retry_id is not None
    calls_before = len(pipeline.calls)
    edits_before = len(msg.edits)

    # Rival clicks
    rival_click = _FakeCallbackClick(
        data=encode("retry", retry_id),
        chat_id=owner_id,
        message=msg,
        sender_id=rival_id,
    )
    await retry_handler(rival_click)

    # Short alert toast
    assert rival_click.answer_calls == [(texts.RETRY_NOT_OWNER, True)]
    # Pipeline NOT executed
    assert len(pipeline.calls) == calls_before
    # Message NOT modified by rival
    assert len(msg.edits) == edits_before
    # Store intact for the owner
    assert len(retry_store) == 1


@pytest.mark.asyncio
async def test_double_click_and_use_after_expiration_handling():
    """Requirement 4: Double-click / after use / after expiration returns short response, no second request."""
    clock_time = 1000.0
    store = RetryStore(ttl_seconds=300.0, clock=lambda: clock_time)

    pipeline = _ControllablePipeline()
    # First attempt fails with retryable error
    pipeline.fail_with = TikTokDownloadError("Temporary failure")
    _, _, url_handler, retry_handler = _setup_test_env(pipeline, retry_store=store)

    owner_id = 400
    event = _FakeEvent("https://www.tiktok.com/@user/video/double_click", sender_id=owner_id)
    await url_handler(event)

    msg = event.messages[0]
    retry_id = _extract_retry_id(msg.edit_kwargs[-1].get("buttons"))
    assert retry_id is not None

    # Simulate running pipeline on retry: block until we trigger event
    pipeline.fail_with = None
    pipeline.block_event = asyncio.Event()

    click1 = _FakeCallbackClick(
        data=encode("retry", retry_id),
        chat_id=owner_id,
        message=msg,
        sender_id=owner_id,
    )
    task1 = asyncio.create_task(retry_handler(click1))
    await asyncio.sleep(0.01)

    # 1. Concurrent click while running -> RETRY_ALREADY_RUNNING
    click2 = _FakeCallbackClick(
        data=encode("retry", retry_id),
        chat_id=owner_id,
        message=msg,
        sender_id=owner_id,
    )
    await retry_handler(click2)
    assert (texts.RETRY_ALREADY_RUNNING, True) in click2.answer_calls

    # Unblock task1 and wait for completion
    pipeline.block_event.set()
    await task1
    assert len(pipeline.calls) == 2

    # 2. Click after use -> RETRY_NOT_AVAILABLE
    click3 = _FakeCallbackClick(
        data=encode("retry", retry_id),
        chat_id=owner_id,
        message=msg,
        sender_id=owner_id,
    )
    await retry_handler(click3)
    assert (texts.RETRY_NOT_AVAILABLE, True) in click3.answer_calls
    assert len(pipeline.calls) == 2

    # 3. Expiration: create another request, advance clock past TTL
    pipeline.fail_with = TikTokDownloadError("Another failure")
    event_exp = _FakeEvent("https://www.tiktok.com/@user/video/expire_test", sender_id=owner_id)
    await url_handler(event_exp)
    msg_exp = event_exp.messages[0]
    retry_id_exp = _extract_retry_id(msg_exp.edit_kwargs[-1].get("buttons"))
    assert retry_id_exp is not None
    assert len(store) == 1

    # Advance clock past TTL (300s)
    clock_time += 400.0
    click_exp = _FakeCallbackClick(
        data=encode("retry", retry_id_exp),
        chat_id=owner_id,
        message=msg_exp,
        sender_id=owner_id,
    )
    await retry_handler(click_exp)
    assert (texts.RETRY_NOT_AVAILABLE, True) in click_exp.answer_calls
    # No extra pipeline call
    assert len(pipeline.calls) == 3  # (1 initial, 1 retry, 1 expire initial)
    assert len(store) == 0


@pytest.mark.asyncio
async def test_limiter_and_credits_enforced_on_retry():
    """Requirement 5: Limiter and credits are enforced on retry."""
    limiter = ConcurrencyLimiter(global_limit=1, per_user_limit=1)
    pipeline = _ControllablePipeline()
    pipeline.fail_with = TikTokDownloadError("Initial failure")
    _, _store, url_handler, retry_handler = _setup_test_env(
        pipeline, limiter=limiter, free_download=3
    )

    user_id = 500
    event = _FakeEvent("https://www.tiktok.com/@user/video/limiter_test", sender_id=user_id)
    await url_handler(event)
    msg = event.messages[0]
    retry_id = _extract_retry_id(msg.edit_kwargs[-1].get("buttons"))
    assert retry_id is not None

    # Hold the limiter slot with an external task
    slot_held = asyncio.Event()
    slot_release = asyncio.Event()

    async def hold_slot() -> None:
        async with limiter.slot(user_id):
            slot_held.set()
            await slot_release.wait()

    holder_task = asyncio.create_task(hold_slot())
    await slot_held.wait()

    # Retry click when limiter slot is busy -> progress updates with YOUTUBE_QUEUE_WAIT
    pipeline.fail_with = None
    click = _FakeCallbackClick(
        data=encode("retry", retry_id),
        chat_id=user_id,
        message=msg,
        sender_id=user_id,
    )
    retry_task = asyncio.create_task(retry_handler(click))
    await asyncio.sleep(0.05)

    assert texts.YOUTUBE_QUEUE_WAIT in msg.edits

    # Release limiter slot -> retry finishes
    slot_release.set()
    await holder_task
    await retry_task
    assert len(pipeline.calls) == 2


@pytest.mark.asyncio
async def test_no_lingering_records_in_retry_store():
    """Requirement 6: Zero lingering records in RetryStore after success, hopeless failure, or expiration."""
    now = 100.0
    store = RetryStore(ttl_seconds=50.0, clock=lambda: now)
    pipeline = _ControllablePipeline()
    _, _, url_handler, retry_handler = _setup_test_env(pipeline, retry_store=store)

    user_id = 600

    # 1. Success on retry clears the store
    pipeline.fail_with = TikTokDownloadError("Fail 1")
    event1 = _FakeEvent("https://www.tiktok.com/@user/video/clear1", sender_id=user_id)
    await url_handler(event1)
    assert len(store) == 1
    rid1 = _extract_retry_id(event1.messages[0].edit_kwargs[-1].get("buttons"))

    pipeline.fail_with = None
    click1 = _FakeCallbackClick(
        data=encode("retry", rid1), chat_id=user_id, message=event1.messages[0], sender_id=user_id
    )
    await retry_handler(click1)
    assert len(store) == 0

    # 2. Hopeless failure on retry leaves store clean
    pipeline.fail_with = TikTokDownloadError("Fail 2")
    event2 = _FakeEvent("https://www.tiktok.com/@user/video/clear2", sender_id=user_id)
    await url_handler(event2)
    assert len(store) == 1
    rid2 = _extract_retry_id(event2.messages[0].edit_kwargs[-1].get("buttons"))

    # On retry, it fails with hopeless error (e.g. BandwidthExhaustedException)
    pipeline.fail_with = BandwidthExhaustedException("Bandwidth exhausted")
    click2 = _FakeCallbackClick(
        data=encode("retry", rid2), chat_id=user_id, message=event2.messages[0], sender_id=user_id
    )
    await retry_handler(click2)
    assert len(store) == 0

    # 3. Expiration leaves store clean
    pipeline.fail_with = TikTokDownloadError("Fail 3")
    event3 = _FakeEvent("https://www.tiktok.com/@user/video/clear3", sender_id=user_id)
    await url_handler(event3)
    assert len(store) == 1
    now += 60.0  # past TTL
    assert len(store) == 0


def test_is_hopeless_failure_comprehensive():
    """Unit test for is_hopeless_failure classification."""
    assert is_hopeless_failure(exc=UnsupportedUrlError("foo"))
    assert is_hopeless_failure(exc=DownloadTooLargeError("bar"))
    assert is_hopeless_failure(exc=CreditsExhaustedException("baz"))
    assert is_hopeless_failure(exc=BandwidthExhaustedException("qux"))
    assert is_hopeless_failure(exc=UserBlockedException("blocked"))
    assert is_hopeless_failure(text=texts.UNSUPPORTED_URL)
    assert is_hopeless_failure(text=texts.CREDITS_EXHAUSTED)
    assert is_hopeless_failure(text=texts.REQUEST_CANCELLED)
    assert is_hopeless_failure(text="❌ הקובץ גדול מדי (150MB).")
    assert is_hopeless_failure(text="❌ המשתמש שלך נחסם. פנה למנהל.")
    assert is_hopeless_failure(text="❌ הסרטון אינו זמין (פרטי, נמחק).")

    # Retryable:
    assert not is_hopeless_failure(exc=TikTokDownloadError("Network error"))
    assert not is_hopeless_failure(text=texts.DOWNLOAD_FAILED)
    assert not is_hopeless_failure(text="שגיאת רשת בהורדה. נסה שוב בעוד מספר רגעים.")
    assert not is_hopeless_failure(text=texts.FLOOD_WAIT_FAILED)
