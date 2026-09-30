"""Tests for M11.7: Transparency, User Information, Cache Indication, Queue Wait, /help, and /ping."""

import asyncio
from pathlib import Path

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.engines.base import BaseEngine
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress import MessageProgressReporter, _is_terminal_text
from media_bot_v2.telegram.router import register_handlers
from tests.test_lessons import _setup_test_db
from tests.test_pipeline import _FakeEngine, _FakeProgress, _FakeUploader


def test_format_download_summary_cache_vs_normal():
    # Cache hit: must include cache indicator and delivery time
    cache_summary = texts.format_download_summary(
        quality_label="1080p HD",
        duration_seconds=125,
        elapsed_seconds=0.4,
        from_cache=True,
    )
    assert texts.DOWNLOAD_DONE in cache_summary
    assert texts.CACHE_DELIVERY_SUMMARY in cache_summary
    assert "⚡ נשלח מהמטמון (מיידי)" in cache_summary
    assert "⏱️ נמסר ב-0:00" in cache_summary
    assert "⏱️ הושלם ב-" not in cache_summary

    # Normal download: must NOT include cache indicator, must include completion time
    normal_summary = texts.format_download_summary(
        quality_label="1080p HD",
        duration_seconds=125,
        size_bytes=15 * 1024 * 1024,
        elapsed_seconds=8.2,
        from_cache=False,
    )
    assert texts.DOWNLOAD_DONE in normal_summary
    assert texts.CACHE_DELIVERY_SUMMARY not in normal_summary
    assert "מהמטמון" not in normal_summary
    assert "⏱️ הושלם ב-0:08" in normal_summary
    assert "⏱️ נמסר ב-" not in normal_summary


async def test_pipeline_cache_hit_shows_cache_indicator(tmp_path):
    session_factory, credits_service, _ = _setup_test_db()
    cache_store = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    key = compute_cache_key("video-cache-m11", "720")
    cache_store.put(key, archive_chat="@archive", message_ids=[101, 102], title="M11 Cached Clip")

    class _MustNotDownload(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path):
            raise AssertionError("Engine download must not run on cache hit")

    progress = _FakeProgress()
    await pipeline.run(
        user_id=1,
        url="https://youtube.com/watch?v=cached1",
        engine=_MustNotDownload(),
        uploader=uploader,
        progress=progress,
        cache=cache_store,
        cache_key=key,
        archive_channel="@archive",
    )

    assert uploader.cached_sends == [("@archive", [101, 102])]
    assert uploader.sent == []

    final_update = progress.updates[-1]
    assert final_update.startswith(texts.DOWNLOAD_DONE)
    assert texts.CACHE_DELIVERY_SUMMARY in final_update
    assert "⚡ נשלח מהמטמון (מיידי)" in final_update
    assert "⏱️ נמסר ב-" in final_update
    assert "⏱️ הושלם ב-" not in final_update


async def test_pipeline_normal_download_does_not_show_cache_indicator(tmp_path):
    session_factory, credits_service, _ = _setup_test_db()
    cache_store = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()
    progress = _FakeProgress()

    await pipeline.run(
        user_id=1,
        url="https://youtube.com/watch?v=fresh1",
        engine=_FakeEngine(),
        uploader=uploader,
        progress=progress,
        cache=cache_store,
        cache_key=compute_cache_key("fresh1", "720"),
        archive_channel="@archive",
    )

    assert len(uploader.sent) > 0
    final_update = progress.updates[-1]
    assert final_update.startswith(texts.DOWNLOAD_DONE)
    assert texts.CACHE_DELIVERY_SUMMARY not in final_update
    assert "מהמטמון" not in final_update
    assert "⏱️ הושלם ב-" in final_update


async def test_pipeline_cache_hit_fallback_to_fresh_does_not_show_cache_indicator(tmp_path):
    session_factory, credits_service, _ = _setup_test_db()
    cache_store = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    # Fail cached send to force fresh fallback
    uploader = _FakeUploader(fail_cached_send=True)
    key = compute_cache_key("video-fallback", "720")
    cache_store.put(key, archive_chat="@archive", message_ids=[201], title="Stale Entry")

    progress = _FakeProgress()
    await pipeline.run(
        user_id=1,
        url="https://youtube.com/watch?v=fallback1",
        engine=_FakeEngine(),
        uploader=uploader,
        progress=progress,
        cache=cache_store,
        cache_key=key,
        archive_channel="@archive",
    )

    # Fresh upload happened
    assert len(uploader.sent) > 0
    final_update = progress.updates[-1]
    assert final_update.startswith(texts.DOWNLOAD_DONE)
    assert texts.CACHE_DELIVERY_SUMMARY not in final_update
    assert "מהמטמון" not in final_update
    assert "⏱️ הושלם ב-" in final_update


async def test_pipeline_failed_download_has_no_cache_indicator(tmp_path):
    _session_factory, credits_service, _ = _setup_test_db()
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _FakeUploader()

    class _FailingEngine(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path):
            raise RuntimeError("network failure")

    progress = _FakeProgress()
    with pytest.raises(RuntimeError):
        await pipeline.run(
            user_id=1,
            url="https://youtube.com/watch?v=fail1",
            engine=_FailingEngine(),
            uploader=uploader,
            progress=progress,
        )

    for update in progress.updates:
        assert texts.CACHE_DELIVERY_SUMMARY not in update
        assert "מהמטמון" not in update


def test_queue_message_classification():
    # Neither texts.QUEUE_WAIT nor texts.YOUTUBE_QUEUE_WAIT should be terminal
    assert not _is_terminal_text(texts.QUEUE_WAIT)
    assert not _is_terminal_text(texts.YOUTUBE_QUEUE_WAIT)
    assert not _is_terminal_text("⏳ ממתין לעיבוד...")
    assert not _is_terminal_text("⏳ ממתין בתור...")


async def test_queue_message_does_not_interrupt_progress_lifecycle():
    edits = []

    class _FakeMsg:
        async def edit(self, text, buttons=None):
            edits.append(text)

        async def respond(self, text, buttons=None):
            edits.append(text)

    msg = _FakeMsg()
    reporter = MessageProgressReporter(msg)

    # Step 1: initial download started
    await reporter.update(texts.DOWNLOAD_STARTED)
    assert reporter._last_text == texts.DOWNLOAD_STARTED
    assert not reporter.is_terminal_completed

    # Step 2: queue wait occurs (rate limiting)
    await reporter.update(texts.QUEUE_WAIT, is_terminal=False)
    assert reporter._last_text == texts.QUEUE_WAIT
    assert not reporter.is_terminal_completed

    # Step 3: slot becomes available, download proceeds
    await reporter.update(texts.DOWNLOADING)
    assert reporter._last_text == texts.DOWNLOADING
    assert not reporter.is_terminal_completed

    # Step 4: uploading
    await reporter.update(texts.UPLOADING)
    assert reporter._last_text == texts.UPLOADING
    assert not reporter.is_terminal_completed

    # Step 5: completed
    await reporter.update(texts.DOWNLOAD_DONE, is_terminal=True)
    assert reporter.is_terminal_completed
    assert edits[-1] == texts.DOWNLOAD_DONE

    # Step 6: subsequent updates are ignored because it's now truly terminal
    await reporter.update("another text after done")
    assert reporter._last_text == texts.DOWNLOAD_DONE


def test_help_text_content_and_structure():
    help_text = texts.HELP

    # Bold title with emoji
    assert help_text.startswith("🤖 **מדריך שימוש בבוט ההורדות**")

    # Supported platforms
    assert "יוטיוב" in help_text
    assert "טיקטוק" in help_text
    assert "אינסטגרם" in help_text
    assert "קישורים ישירים" in help_text

    # Usage instructions including cancel and retry
    assert "ביטול" in help_text
    assert "נסה שוב" in help_text

    # Quality tips
    assert "איכות" in help_text
    assert "הגדרות" in help_text

    # Commands listed
    assert "/start" in help_text
    assert "/help" in help_text
    assert "/settings" in help_text
    assert "/ping" in help_text

    # Formatting rules: reasonable length for Telegram, no long dividers, no tech jargon
    assert 300 < len(help_text) < 2500
    for divider in ("---", "___", "===", "───", "──", "***"):
        assert divider not in help_text

    tech_jargon = ["api", "http", "json", "regex", "database", "worker", "backend", "token"]
    for term in tech_jargon:
        assert term not in help_text.lower()


def test_ping_text_structure():
    ping_result = texts.PING_RESULT.format(ms=45)
    assert ping_result == "🟢 הבוט פעיל · זמן תגובה: 45ms"
    assert len(ping_result) < 100
    assert "---" not in ping_result


async def test_ping_handler_measured_execution():
    client = TelegramClient(MemorySession(), 1, "hash")
    session_factory, credits_service, _ = _setup_test_db()
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=Path("/tmp/m11-test"))

    register_handlers(
        client,
        session_factory=session_factory,
        credits_service=credits_service,
        free_download=3,
        pipeline=pipeline,
        archive_channel=None,
        max_download_size=4 * 1024 * 1024 * 1024,
    )

    ping_callback = None
    for callback, _event in client.list_event_handlers():
        if getattr(callback, "__name__", "") == "ping_handler":
            ping_callback = callback
            break

    assert ping_callback is not None, "ping handler must be registered"

    edits = []

    class _FakeMessage:
        async def edit(self, text, **kwargs):
            edits.append(text)

    sent_messages = []

    class _FakeEvent:
        async def respond(self, text, **kwargs):
            sent_messages.append(text)
            await asyncio.sleep(0.01)  # small simulated latency
            return _FakeMessage()

    await ping_callback(_FakeEvent())

    assert len(sent_messages) == 1
    assert sent_messages[0] == texts.PING_MESSAGE
    assert len(edits) == 1
    assert edits[0].startswith("🟢 הבוט פעיל · זמן תגובה: ")
    assert edits[0].endswith("ms")
