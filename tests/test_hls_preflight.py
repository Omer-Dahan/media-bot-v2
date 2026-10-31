from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base
from media_bot_v2.engines.base import DownloadResult
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.hls import HlsUnsupportedError, HlsWindowInfo
from media_bot_v2.telegram import router, texts
from media_bot_v2.telegram.callback_data import encode
from media_bot_v2.telegram.router import register_handlers


class _Sender:
    first_name = "Test"
    username = "tester"


class _Message:
    _next_id = 0

    def __init__(self) -> None:
        type(self)._next_id += 1
        self.id = type(self)._next_id
        self.edits: list[str] = []
        self.edit_kwargs: list[dict] = []

    async def edit(self, text: str = "", **kwargs):
        self.edits.append(text)
        self.edit_kwargs.append(kwargs)
        return self


class _UrlEvent:
    def __init__(self, url: str, sender_id: int = 1) -> None:
        self.raw_text = url
        self.sender_id = sender_id
        self.chat_id = 10
        self.sender = _Sender()
        self.responses: list[tuple[str, dict]] = []
        self.messages: list[_Message] = []

    async def respond(self, text: str, **kwargs):
        message = _Message()
        self.responses.append((text, kwargs))
        self.messages.append(message)
        return message


class _CallbackEvent:
    def __init__(self, data: bytes, message: _Message, sender_id: int = 1) -> None:
        self.data = data
        self.sender_id = sender_id
        self.chat_id = 10
        self.message_id = message.id
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answers.append((text, alert))

    async def edit(self, text: str = "", **kwargs):
        await self.message.edit(text, **kwargs)
        return self.message


class _Pipeline:
    def __init__(self, *, invoke_engine: bool = False) -> None:
        self.calls: list[dict] = []
        self.invoke_engine = invoke_engine

    async def run(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self.invoke_engine:
            await kwargs["engine"].download(
                kwargs["url"], dest_dir=Path("/tmp/media-bot-v2-hls-preflight")
            )


class _HangingPipeline(_Pipeline):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, **kwargs) -> None:
        self.calls.append(kwargs)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _window(duration: float = 1200, estimated_bytes: int = 245 * 1024 * 1024) -> HlsWindowInfo:
    return HlsWindowInfo(
        segment_count=600,
        window_duration=duration,
        first_media_sequence=100,
        last_media_sequence=699,
        selected_variant_uri="720p/index.m3u8",
        selected_variant_resolution=(1280, 720),
        selected_variant_bandwidth=1_650_000,
        has_program_date_time=False,
        estimated_bytes=estimated_bytes,
    )


def _make_router(pipeline, **kwargs):
    client = TelegramClient(MemorySession(), 1, "hash")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    credits = CreditsService(sessions, enable_vip=False, owner_ids=[], free_bandwidth=1)
    register_handlers(
        client,
        session_factory=sessions,
        credits_service=credits,
        free_download=3,
        pipeline=pipeline,
        archive_channel=None,
        max_download_size=4 * 1024 * 1024 * 1024,
        **kwargs,
    )
    return client


def _handler(client, event_type, sample: bytes | None = None):
    for callback, event in client.list_event_handlers():
        if not isinstance(event, event_type):
            continue
        if event_type is events.NewMessage and event.pattern is None:
            return callback
        if sample is not None and getattr(event, "match", None) and event.match(sample):
            return callback
    raise AssertionError(f"handler not found for {sample!r}")


async def test_live_url_shows_preflight_without_downloading_segments(monkeypatch):
    pipeline = _Pipeline()
    monkeypatch.setattr(router, "analyze_hls_window", lambda *args, **kwargs: _window())
    client = _make_router(pipeline)
    event = _UrlEvent("https://live.example/720p/index.m3u8")

    await _handler(client, events.NewMessage)(event)

    assert pipeline.calls == []
    text, response = event.responses[0]
    assert text == (
        "🔴 זוהה שידור חי\n"
        "זמין להורדה: 20:00 (600 מקטעים) · 720p · ~245MB · ~2 קרדיטים\n\n"
        "בחר כמה להוריד:"
    )
    labels = [button.text for row in response["buttons"] for button in row]
    assert labels == ["כל החלון (20:00)", "10 דקות אחרונות", "ביטול"]


@pytest.mark.parametrize(
    ("choice", "expected"), [("all", None), ("600", 600), ("1800", 1800)]
)
async def test_range_callback_passes_window_seconds(monkeypatch, choice, expected):
    monkeypatch.setattr(router, "analyze_hls_window", lambda *args, **kwargs: _window(3601))
    received: list[float | None] = []

    def fake_download(url, dest_dir, max_size, cancel_token, window_seconds=None, **kwargs):
        received.append(window_seconds)
        return DownloadResult(file_paths=[])

    monkeypatch.setattr(router, "download_hls_window", fake_download)
    pipeline = _Pipeline(invoke_engine=True)
    client = _make_router(pipeline)
    url_event = _UrlEvent("https://live.example/index.m3u8")
    await _handler(client, events.NewMessage)(url_event)

    callback = _CallbackEvent(encode("hlsc", choice), url_event.messages[0])
    await _handler(client, events.CallbackQuery, b"hlsc:600")(callback)

    assert received == [expected]


async def test_non_owner_cannot_start_live_download(monkeypatch):
    monkeypatch.setattr(router, "analyze_hls_window", lambda *args, **kwargs: _window())
    pipeline = _Pipeline()
    client = _make_router(pipeline)
    url_event = _UrlEvent("https://live.example/index.m3u8")
    await _handler(client, events.NewMessage)(url_event)

    callback = _CallbackEvent(encode("hlsc", "all"), url_event.messages[0], sender_id=2)
    await _handler(client, events.CallbackQuery, b"hlsc:all")(callback)

    assert callback.answers == [(texts.HLS_CONFIRM_NOT_OWNER, True)]
    assert pipeline.calls == []


async def test_expired_live_confirmation_is_refused_and_edited(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(router.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(router, "analyze_hls_window", lambda *args, **kwargs: _window())
    pipeline = _Pipeline()
    client = _make_router(pipeline, hls_confirm_ttl_seconds=600)
    url_event = _UrlEvent("https://live.example/index.m3u8")
    await _handler(client, events.NewMessage)(url_event)
    now[0] = 601.0

    callback = _CallbackEvent(encode("hlsc", "all"), url_event.messages[0])
    await _handler(client, events.CallbackQuery, b"hlsc:all")(callback)

    assert callback.answers == [(texts.HLS_CONFIRM_EXPIRED_TOAST, True)]
    assert url_event.messages[0].edits[-1] == texts.HLS_CONFIRM_EXPIRED
    assert pipeline.calls == []


async def test_finite_vod_keeps_direct_pipeline_path(monkeypatch):
    def finite_playlist(*args, **kwargs):
        raise HlsUnsupportedError("malformed", "playlist is finite, not a live window")

    monkeypatch.setattr(router, "analyze_hls_window", finite_playlist)
    pipeline = _Pipeline()
    client = _make_router(pipeline)
    event = _UrlEvent("https://vod.example/movie.m3u8")

    await _handler(client, events.NewMessage)(event)

    assert len(pipeline.calls) == 1
    assert isinstance(pipeline.calls[0]["engine"], DirectEngine)
    assert event.responses[0][0] == texts.DOWNLOAD_STARTED


async def test_live_window_download_uses_cancel_registry_and_unregisters(monkeypatch):
    monkeypatch.setattr(router, "analyze_hls_window", lambda *args, **kwargs: _window())
    pipeline = _HangingPipeline()
    client = _make_router(pipeline)
    url_event = _UrlEvent("https://live.example/index.m3u8")
    await _handler(client, events.NewMessage)(url_event)
    message = url_event.messages[0]

    range_click = _CallbackEvent(encode("hlsc", "600"), message)
    range_task = asyncio.create_task(
        _handler(client, events.CallbackQuery, b"hlsc:600")(range_click)
    )
    await asyncio.wait_for(pipeline.started.wait(), timeout=2)

    cancel_click = _CallbackEvent(encode("cancel"), message)
    await _handler(client, events.CallbackQuery, b"cancel")(cancel_click)
    await asyncio.wait_for(range_task, timeout=2)

    assert pipeline.cancelled is True
    second_click = _CallbackEvent(encode("cancel"), message)
    await _handler(client, events.CallbackQuery, b"cancel")(second_click)
    assert second_click.answers == [(texts.CANCEL_NOT_ACTIVE, True)]
