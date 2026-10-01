"""M11.4: the ❌ cancel button on the progress message.

These tests drive `register_handlers`' real `url_handler`/`cancel_handler`
with fake Telethon events and a fake `pipeline` that just hangs until
cancelled - the point here is the *router's* wiring (button attached while
active, ownership check, no-op after completion, no leftover background
task), not `DownloadPipeline`'s own cancellation behavior (covered directly,
with a real pipeline, in test_pipeline.py).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.callback_data import encode
from media_bot_v2.telegram.router import register_handlers


class _FakeSender:
    first_name = "Test User"
    username = "testuser"


class _FakeMessage:
    _next_id = 0

    def __init__(self) -> None:
        type(self)._next_id += 1
        self.id = type(self)._next_id
        self.edits: list[str] = []
        self.edit_kwargs: list[dict] = []

    async def edit(self, text: str = "", *args, **kwargs) -> None:
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)


class _FakeEvent:
    """A NewMessage event carrying a direct-download URL (no YouTube/TikTok/
    Instagram host matching needed, so no extra engine setup)."""

    def __init__(self, text: str, sender_id: int):
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.sender = _FakeSender()
        self.responses: list[str] = []
        self.respond_kwargs: list[dict] = []
        self.messages: list[_FakeMessage] = []

    async def respond(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            self.responses.append(args[0])
        self.respond_kwargs.append(kwargs)
        msg = _FakeMessage()
        self.messages.append(msg)
        return msg


class _FakeCancelClick:
    def __init__(self, *, chat_id: int, message_id: int, sender_id: int):
        self.data = encode("cancel")
        self.chat_id = chat_id
        self.message_id = message_id
        self.sender_id = sender_id
        self.answer_calls: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answer_calls.append((text, alert))


class _HangingPipeline:
    """`run()` reports readiness then hangs until its task is cancelled -
    simulates a request in the middle of downloading/uploading."""

    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.cancelled = False
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> None:
        self.calls.append(kwargs)
        self.ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _CompletingPipeline:
    """`run()` returns immediately - simulates a request that already finished."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _make_router(*, pipeline):
    client = TelegramClient(MemorySession(), 1, "hash")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=1)
    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=3,
        pipeline=pipeline,
        archive_channel=None,
        max_download_size=4 * 1024 * 1024 * 1024,
    )
    return client


def _find_url_handler(client: TelegramClient):
    for callback, event in client.list_event_handlers():
        if isinstance(event, events.NewMessage) and event.pattern is None:
            return callback
    raise AssertionError("url_handler (bare NewMessage handler) not registered")


def _find_cancel_handler(client: TelegramClient):
    for callback, event in client.list_event_handlers():
        if isinstance(event, events.CallbackQuery) and getattr(event, "match", None) and event.match(b"cancel"):
            return callback
    raise AssertionError("cancel_handler (CallbackQuery ^cancel$) not registered")


async def test_owner_cancel_stops_the_task_and_leaves_no_background_work():
    pipeline = _HangingPipeline()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    cancel_handler = _find_cancel_handler(client)
    event = _FakeEvent("http://files.example.org/movie.bin", sender_id=1)

    tasks_before = {t for t in asyncio.all_tasks() if not t.done()}
    handler_task = asyncio.create_task(url_handler(event))
    await asyncio.wait_for(pipeline.ready.wait(), timeout=5)

    # The button is attached to the progress message while the request is active.
    message = event.messages[0]
    (row,) = event.respond_kwargs[0]["buttons"]
    assert [b.text for b in row] == [texts.CANCEL_BUTTON]

    click = _FakeCancelClick(chat_id=event.chat_id, message_id=message.id, sender_id=1)
    await cancel_handler(click)

    # The owner's click gets an immediate toast and message update...
    assert click.answer_calls == [(texts.CANCEL_TOAST, False)]
    assert message.edits[-1] == texts.REQUEST_CANCELLED
    # ...with the cancel button cleared, not left dangling on a dead request.
    # `buttons` must be `None` *explicitly present in kwargs* - Telethon only
    # overrides `reply_markup` when `buttons` appears in kwargs at all; if it
    # were simply omitted (the bug in the prior round's fix), Telethon would
    # re-inject the message's existing inline keyboard and the button would
    # stay live on a now-dead request. An empty ReplyInlineMarkup([]) looks
    # like the obvious "cleared" shape but is rejected by live Telegram with
    # ReplyMarkupInvalidError, and ReplyKeyboardHide (the prior round's fix)
    # clears the bottom *reply* keyboard, not an inline one - see
    # telegram/progress.py's module-level note above `_buttons_for_new_message`.
    assert "buttons" in message.edit_kwargs[-1]
    assert message.edit_kwargs[-1]["buttons"] is None

    # ...and actually reaches the pipeline's own task.
    await asyncio.wait_for(handler_task, timeout=5)
    assert pipeline.cancelled is True

    # A second click on the now-finished request is inert, not a crash or a
    # second cancellation of anything.
    second_click = _FakeCancelClick(chat_id=event.chat_id, message_id=message.id, sender_id=1)
    await cancel_handler(second_click)
    assert second_click.answer_calls == [(texts.CANCEL_NOT_ACTIVE, True)]
    assert message.edits[-1] == texts.REQUEST_CANCELLED  # unchanged by the second click

    leftover = {t for t in asyncio.all_tasks() if not t.done()} - tasks_before
    assert leftover == set()


async def test_other_users_click_does_not_cancel_anything():
    pipeline = _HangingPipeline()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    cancel_handler = _find_cancel_handler(client)
    event = _FakeEvent("http://files.example.org/movie.bin", sender_id=1)

    handler_task = asyncio.create_task(url_handler(event))
    await asyncio.wait_for(pipeline.ready.wait(), timeout=5)
    message = event.messages[0]

    rival_click = _FakeCancelClick(chat_id=event.chat_id, message_id=message.id, sender_id=999)
    await cancel_handler(rival_click)

    assert rival_click.answer_calls == [(texts.CANCEL_NOT_OWNER, True)]
    assert pipeline.cancelled is False
    assert message.edits == []  # untouched - still just the initial DOWNLOAD_STARTED send

    # Clean up: the real owner can still cancel it afterwards.
    owner_click = _FakeCancelClick(chat_id=event.chat_id, message_id=message.id, sender_id=1)
    await cancel_handler(owner_click)
    await asyncio.wait_for(handler_task, timeout=5)
    assert pipeline.cancelled is True


async def test_cancel_click_with_no_active_request_is_a_short_no_op():
    pipeline = _CompletingPipeline()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    cancel_handler = _find_cancel_handler(client)
    event = _FakeEvent("http://files.example.org/movie.bin", sender_id=1)

    await url_handler(event)  # completes immediately - nothing left to cancel
    message = event.messages[0]

    click = _FakeCancelClick(chat_id=event.chat_id, message_id=message.id, sender_id=1)
    await cancel_handler(click)

    assert click.answer_calls == [(texts.CANCEL_NOT_ACTIVE, True)]
    assert message.edits == []  # the click did not touch the message
