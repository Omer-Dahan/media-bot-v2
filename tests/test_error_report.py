"""Tests for M12.1: error reports to the archive/error channel.

Covers the eight scenarios from the triage's section 3.9:
1. A domain failure sends exactly one send_message to the channel, with no
   `buttons` kwarg, after the user already saw the terminal failure message.
2. A send_message failure is swallowed (WARNING only); the download path
   finishes normally.
3. Cancellation, credits-exhausted and bandwidth-exhausted are never reported.
4. A 50KB log full of `&<>"` truncates to <=4096 UTF-16 units with valid HTML
   and an attached .txt of the full log.
5. Dedup: two identical failures within the window produce one report; a
   different `kind` produces a second one.
6. Two concurrent requests never mix log lines in each other's buffer.
7. A log line written from `executor.run_in_thread` reaches the buffer.
8. A leaked `api_key=...` in the log is masked in the sent report.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
)
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base
from media_bot_v2.engines.base import DownloadTooLargeError
from media_bot_v2.executor import run_in_thread
from media_bot_v2.logging_setup import RequestLogHandler, current_request_log, request_log_scope
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.captions import MESSAGE_LIMIT, utf16_units
from media_bot_v2.telegram.error_report import ErrorReporter, classify_kind
from media_bot_v2.telegram.router import register_handlers
from tests.fakes_telegram import FakeTelegramClient

CHANNEL = -1009988776655


@pytest.fixture(autouse=True)
def _request_log_handler_on_root():
    """Every test here needs the per-request log handler installed on the
    root logger - configure_logging() isn't called in the test process, and
    other test modules' fixtures restore the root logger to whatever state
    they found it in, so this cannot be assumed to already be there."""
    root = logging.getLogger()
    handler = RequestLogHandler()
    previous_level = root.level
    root.addHandler(handler)
    if root.level == 0 or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    yield
    root.removeHandler(handler)
    root.setLevel(previous_level)


async def _drain(reporter: ErrorReporter) -> None:
    """Awaits every task report() has scheduled so far - report() itself
    never awaits anything (it only schedules a background task)."""
    tasks = list(reporter._tasks)
    if tasks:
        await asyncio.gather(*tasks)


class _FakeMessage:
    _next_id = 1000

    def __init__(self, chat_id: int) -> None:
        type(self)._next_id += 1
        self.id = type(self)._next_id
        self.chat_id = chat_id
        self.edits: list[str] = []

    async def edit(self, text: str = "", *args: Any, **kwargs: Any) -> _FakeMessage:
        self.edits.append(text)
        return self


class _FakeEvent:
    def __init__(self, text: str, sender_id: int) -> None:
        self.raw_text = text
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.is_private = True
        self.sender = type("S", (), {"first_name": "Tester", "username": "tester"})()
        self.messages: list[_FakeMessage] = []

    async def respond(self, *args: Any, **kwargs: Any) -> _FakeMessage:
        msg = _FakeMessage(chat_id=self.chat_id)
        self.messages.append(msg)
        return msg


class _ControllablePipeline:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    async def run(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.fail_with is not None:
            raise self.fail_with


def _setup(
    client: FakeTelegramClient, pipeline: _ControllablePipeline, *, error_reporter: ErrorReporter | None
) -> Any:
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
        error_reporter=error_reporter,
    )

    url_handler = None
    from telethon import events

    for callback, event in client.list_event_handlers():
        if isinstance(event, events.NewMessage) and event.pattern is None:
            url_handler = callback
    assert url_handler is not None, "url_handler not found"
    return url_handler


def _channel_sends(client: FakeTelegramClient) -> list:
    return [c for c in client.calls_to("send_message") if c.args[0] == CHANNEL]


def _channel_files(client: FakeTelegramClient) -> list:
    return [c for c in client.calls_to("send_file") if c.args[0] == CHANNEL]


# 1. A domain failure reports exactly once, with no `buttons` kwarg, after
# the user already has the terminal failure message.
async def test_domain_failure_sends_one_channel_report_without_buttons():
    pipeline = _ControllablePipeline()
    pipeline.fail_with = DownloadTooLargeError("custom too-large message")
    client = FakeTelegramClient()
    reporter = ErrorReporter(client, CHANNEL)
    url_handler = _setup(client, pipeline, error_reporter=reporter)

    event = _FakeEvent("https://www.tiktok.com/@user/video/111", sender_id=42)
    await url_handler(event)
    await _drain(reporter)

    msg = event.messages[0]
    assert msg.edits[-1] == "custom too-large message"

    sends = _channel_sends(client)
    assert len(sends) == 1
    assert "buttons" not in sends[0].kwargs
    assert "reply_markup" not in sends[0].kwargs
    assert "#tiktok" in sends[0].args[1]


# 2. A send_message failure is only ever a WARNING; the handler finishes
# normally, with no exception escaping.
async def test_failed_send_is_warning_only_and_download_path_still_finishes(caplog):
    pipeline = _ControllablePipeline()
    pipeline.fail_with = DownloadTooLargeError("too big")
    client = FakeTelegramClient()
    reporter = ErrorReporter(client, CHANNEL)
    url_handler = _setup(client, pipeline, error_reporter=reporter)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Telegram is down")

    client.send_message = _boom

    event = _FakeEvent("https://www.tiktok.com/@user/video/222", sender_id=43)
    with caplog.at_level(logging.WARNING):
        await url_handler(event)  # must not raise
        await _drain(reporter)  # must not raise

    msg = event.messages[0]
    assert msg.edits[-1] == "too big"
    assert any("Error report to archive failed" in r.message for r in caplog.records)


# 3. Cancellation, credits-exhausted and bandwidth-exhausted are never
# reported.
async def test_cancellation_credits_and_bandwidth_are_never_reported():
    pipeline = _ControllablePipeline()
    client = FakeTelegramClient()
    reporter = ErrorReporter(client, CHANNEL)
    url_handler = _setup(client, pipeline, error_reporter=reporter)

    pipeline.fail_with = CreditsExhaustedException(texts.CREDITS_EXHAUSTED)
    await url_handler(_FakeEvent("https://www.tiktok.com/@user/video/333", sender_id=44))
    await _drain(reporter)
    assert _channel_sends(client) == []

    pipeline.fail_with = BandwidthExhaustedException("over the daily cap")
    await url_handler(_FakeEvent("https://www.tiktok.com/@user/video/444", sender_id=45))
    await _drain(reporter)
    assert _channel_sends(client) == []

    pipeline.fail_with = asyncio.CancelledError()
    await url_handler(_FakeEvent("https://www.tiktok.com/@user/video/555", sender_id=46))
    await _drain(reporter)
    assert _channel_sends(client) == []


# 4. A 50KB log full of HTML-special characters truncates correctly and
# attaches the full log.
async def test_large_log_truncates_under_limit_and_attaches_full_log():
    client = FakeTelegramClient()
    reporter = ErrorReporter(client, CHANNEL)

    # Stays under RequestLogBuffer's own 300-line cap (so every line survives
    # into current_request_log()) while still totalling ~50KB, so the
    # *message* truncation (not the buffer's own line cap) is what's tested.
    padding = "x" * 200
    with request_log_scope(user_id=99, url="https://evil.example/x", platform="direct") as buffer:
        for i in range(200):
            buffer.append(
                f'line {i}: <tag> & "quoted" value api_key=notasecret{i} {padding}'
            )
        reporter.report(
            user_id=99,
            user_display="Tester",
            chat_id=1234,
            url="https://evil.example/x",
            platform="direct",
            quality=None,
            kind="internal",
            user_text=texts.DOWNLOAD_FAILED,
            exc=RuntimeError("boom"),
            started_at=time.monotonic(),
        )
    await _drain(reporter)

    sends = _channel_sends(client)
    assert len(sends) == 1
    text = sends[0].args[1]
    assert utf16_units(text) <= MESSAGE_LIMIT
    assert "line 0:" not in text  # the head was dropped, the tail was kept

    files = _channel_files(client)
    assert len(files) == 1
    assert files[0].kwargs.get("file_name", "").startswith("error_log_99_")
    attached_text = files[0].args[1].getvalue().decode("utf-8")
    assert "line 0:" in attached_text  # full log is in the attachment


# 5. Dedup within the window; a different kind reports again.
async def test_dedup_same_kind_once_different_kind_twice():
    client = FakeTelegramClient()
    reporter = ErrorReporter(client, CHANNEL, dedup_seconds=600.0)

    def _report(kind: str) -> None:
        reporter.report(
            user_id=7,
            user_display="U",
            chat_id=1234,
            url="https://example.com/a",
            platform="direct",
            quality=None,
            kind=kind,
            user_text=texts.DOWNLOAD_FAILED,
            exc=RuntimeError("same failure"),
            started_at=time.monotonic(),
        )

    _report("internal")
    _report("internal")
    await _drain(reporter)
    assert len(_channel_sends(client)) == 1

    _report("timeout")
    await _drain(reporter)
    assert len(_channel_sends(client)) == 2


# 6. Two concurrent requests keep separate buffers.
async def test_concurrent_requests_do_not_mix_log_buffers():
    results: dict[str, str] = {}
    started = {"A": asyncio.Event(), "B": asyncio.Event()}
    release = asyncio.Event()

    async def _run(tag: str) -> None:
        with request_log_scope(user_id=1, url=f"https://example.com/{tag}", platform="direct"):
            logging.getLogger("media_bot_v2.test_error_report").info("line-from-%s", tag)
            started[tag].set()
            await release.wait()
            results[tag] = current_request_log()

    task_a = asyncio.create_task(_run("A"))
    task_b = asyncio.create_task(_run("B"))
    await started["A"].wait()
    await started["B"].wait()
    release.set()
    await asyncio.gather(task_a, task_b)

    assert "line-from-A" in results["A"]
    assert "line-from-B" not in results["A"]
    assert "line-from-B" in results["B"]
    assert "line-from-A" not in results["B"]


# 7. A log line written from a worker thread (executor.run_in_thread) reaches
# the active request's buffer.
async def test_log_line_from_run_in_thread_reaches_the_buffer():
    def _log_from_worker_thread() -> None:
        logging.getLogger("media_bot_v2.test_error_report").warning("from-worker-thread")

    with request_log_scope(user_id=1, url="https://example.com/x", platform="direct") as buffer:
        await run_in_thread(_log_from_worker_thread)
        assert "from-worker-thread" in buffer.text()


# 8. A leaked api_key in the log is masked before it reaches the sent report.
async def test_leaked_api_key_in_log_is_masked_in_report():
    client = FakeTelegramClient()
    reporter = ErrorReporter(client, CHANNEL)

    with request_log_scope(user_id=5, url="https://ytmp3.example/auth", platform="youtube") as buffer:
        buffer.append("ytmp3 auth request failed: https://gamma.example/api/v1/auth?api_key=TOPSECRET123")
        reporter.report(
            user_id=5,
            user_display="U",
            chat_id=1234,
            url="https://ytmp3.example/auth",
            platform="youtube",
            quality=None,
            kind="provider",
            user_text=texts.YOUTUBE_GENERIC_FAILURE,
            exc=RuntimeError("ytmp3 auth request failed: api_key=TOPSECRET123"),
            started_at=time.monotonic(),
        )
    await _drain(reporter)

    sends = _channel_sends(client)
    assert len(sends) == 1
    text = sends[0].args[1]
    assert "TOPSECRET123" not in text
    assert "api_key=***" in text


def test_classify_kind_covers_the_documented_kinds():

    from media_bot_v2.engines.base import NotMediaContentError

    assert classify_kind(NotMediaContentError()) == "not_media"
    assert classify_kind(DownloadTooLargeError("x")) == "too_large"
    assert classify_kind(TimeoutError()) == "timeout"
    assert classify_kind(None) == "internal"
    assert classify_kind(RuntimeError("anything")) == "internal"
