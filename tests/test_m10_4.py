"""Tests for M10.4: Fix regressions and user messages that still break."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from telethon.errors import FloodWaitError, RPCError

from media_bot_v2.engines.youtube import YouTubeEngine
from media_bot_v2.telegram import parallel_upload, texts
from media_bot_v2.telegram.progress import (
    MessageProgressReporter,
    UploadProgress,
    _is_terminal_text,
)
from tests.test_parallel_upload import PART, PartServer, make_file
from tests.test_router import (
    _FakeCallbackEvent,
    _find_url_handler,
    _find_ytq_handler,
    _make_router,
    _QualityEvent,
)


# -----------------------------------------------------------------------------
# 1. Delayed download progress update does not overwrite final completion message
# -----------------------------------------------------------------------------
async def test_delayed_non_terminal_update_does_not_overwrite_completion():
    """Finding 1 / Requirement 1 & 2: A delayed progress update (e.g. from yt-dlp hook)
    must not overwrite a terminal completion message ("הושלם ✅") when it wakes up."""
    edits = []

    msg = MagicMock()

    async def fake_edit(t, **kwargs):
        edits.append(t)

    msg.edit = fake_edit

    delay_event = asyncio.Event()

    async def fake_sleep(sec):
        # Pause task until delay_event is set
        await delay_event.wait()

    reporter = MessageProgressReporter(msg, sleep_func=fake_sleep)

    # Simulate non-terminal update that hits a flood wait / delay
    msg_edit_mock = AsyncMock(side_effect=[FloodWaitError(None, capture=1), None])

    async def edit_with_flood(t, **kwargs):
        await msg_edit_mock(t, **kwargs)
        edits.append(t)

    msg.edit = edit_with_flood

    # Start non-terminal update (e.g. 45% download) in background
    dl_text = f"{texts.DOWNLOADING}\n45% (10MB/20MB)\n⚡ מהירות: 1.0MB/s\n⏱️ זמן משוער: 00:10"
    task = asyncio.create_task(reporter.update(dl_text, is_terminal=False))

    # Yield so task starts and enters fake_sleep
    await asyncio.sleep(0.01)

    # In the meantime, download finishes and pipeline writes completion message
    await reporter.update(texts.DOWNLOAD_DONE, is_terminal=True)
    assert reporter._last_text == texts.DOWNLOAD_DONE

    # Now the delayed non-terminal update wakes up
    delay_event.set()
    await task

    # Verify: delayed update DID NOT overwrite DOWNLOAD_DONE
    assert reporter._last_text == texts.DOWNLOAD_DONE
    assert edits[-1] == texts.DOWNLOAD_DONE
    assert edits.count(texts.DOWNLOAD_DONE) == 1
    assert dl_text not in edits


# -----------------------------------------------------------------------------
# 2. Progress classification and no new message / no deletion on failure
# -----------------------------------------------------------------------------
def test_progress_text_classification_robust():
    """Finding 2 / Requirement 7: All progress strings are correctly classified
    as non-terminal, while completion, error, and quota strings are terminal."""
    # Non-terminal examples:
    assert not _is_terminal_text(texts.DOWNLOAD_STARTED)
    assert not _is_terminal_text(texts.DOWNLOADING)
    assert not _is_terminal_text(f"{texts.DOWNLOADING}\n45% (10MB/20MB)\n⚡ מהירות: 1.0MB/s\n⏱️ זמן משוער: 00:10")
    assert not _is_terminal_text(texts.DOWNLOADING_QUALITY.format(name="1080p HD"))
    assert not _is_terminal_text(texts.DOWNLOADING_AUDIO)
    assert not _is_terminal_text(texts.PROCESSING)
    assert not _is_terminal_text(texts.UPLOADING)
    assert not _is_terminal_text("מעלה לטלגרם... 45%")
    assert not _is_terminal_text(texts.DOWNLOAD_FROM_CACHE)
    assert not _is_terminal_text(texts.YOUTUBE_QUEUE_WAIT)
    assert not _is_terminal_text(texts.FLOOD_WAIT_MESSAGE.format(seconds=15))

    # Terminal examples:
    assert _is_terminal_text(texts.DOWNLOAD_DONE)
    assert _is_terminal_text(texts.format_playlist_trimmed(2, 5))
    assert _is_terminal_text(texts.DOWNLOAD_FAILED)
    assert _is_terminal_text(texts.FLOOD_WAIT_FAILED)
    assert _is_terminal_text(texts.REQUEST_TIMEOUT_EXCEEDED)
    assert _is_terminal_text(texts.CREDITS_EXHAUSTED)
    assert _is_terminal_text(texts.BANDWIDTH_EXHAUSTED)
    assert _is_terminal_text(texts.UNSUPPORTED_URL)
    assert _is_terminal_text("❌ שגיאה כללית")
    assert _is_terminal_text("⏱️ שגיאת זמן")
    assert _is_terminal_text(texts.YOUTUBE_GENERIC_FAILURE)
    assert _is_terminal_text(texts.INSTAGRAM_GENERIC_FAILURE)
    assert _is_terminal_text("טקסט כלשהו", buttons=[["button"]])


async def test_progress_failure_does_not_send_new_message_or_delete():
    """Finding 2: Failure on a non-terminal progress update (ConnectionError,
    prolonged flood wait, etc.) does NOT send a new message and does NOT delete the message."""
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=ConnectionError("connection dropped"))
    msg.respond = AsyncMock()
    msg.delete = AsyncMock()

    reporter = MessageProgressReporter(msg)
    dl_text = f"{texts.DOWNLOADING}\n45% (10MB/20MB)"
    await reporter.update(dl_text, is_terminal=False)

    # Must NOT send fallback message or delete original
    msg.respond.assert_not_called()
    msg.delete.assert_not_called()

    # Prolonged flood wait on non-terminal update is skipped cleanly
    msg.edit.side_effect = FloodWaitError(None, capture=120)
    await reporter.update(f"{texts.DOWNLOADING}\n50% (11MB/20MB)", is_terminal=False)
    msg.respond.assert_not_called()
    msg.delete.assert_not_called()


# -----------------------------------------------------------------------------
# 3. Message deleted by user: single backup message, repointing, no spam
# -----------------------------------------------------------------------------
async def test_deleted_message_creates_single_backup_and_subsequent_updates_edit_new_message():
    """Finding 3 / Requirement 3: When user deletes the original progress message,
    fallback delivers exactly one replacement message, repoints self._message to it,
    and subsequent updates edit the replacement message without spamming."""
    msg1 = MagicMock()
    msg1.edit = AsyncMock(side_effect=RPCError(None, message="MESSAGE_ID_INVALID"))
    msg1.delete = AsyncMock()

    msg2 = MagicMock()
    msg2.edit = AsyncMock(return_value=None)
    msg2.delete = AsyncMock()
    msg1.respond = AsyncMock(return_value=msg2)

    reporter = MessageProgressReporter(msg1)

    # 1. Non-terminal updates fail against deleted message but do NOT spam new messages
    for pct in (10, 20, 30, 40, 50):
        await reporter.update(f"{texts.DOWNLOADING}\n{pct}%", is_terminal=False)
    assert msg1.respond.call_count == 0

    # 2. Terminal update arrives -> triggers single fallback delivery
    await reporter.update(texts.DOWNLOAD_DONE, is_terminal=True)
    assert msg1.respond.call_count == 1
    msg1.respond.assert_called_once_with(texts.DOWNLOAD_DONE)

    # 3. reporter._message is repointed to msg2
    assert reporter._message is msg2

    # 4. Any subsequent update with new content edits msg2 directly and produces zero new messages
    new_text = "הושלם בהצלחה ✅"
    await reporter.update(new_text, is_terminal=True)
    assert msg1.respond.call_count == 1
    assert msg2.edit.call_count == 1
    msg2.edit.assert_called_with(new_text)


# -----------------------------------------------------------------------------
# 4. Fallback delivery failure deletes stale message to prevent misleading status (M10.5)
# -----------------------------------------------------------------------------
async def test_fallback_delivery_failure_deletes_stale_message():
    """M10.5 Item 2: If edit fails AND fallback respond fails (e.g. flood wait
    exceeding max_wait_seconds), the stale progress message IS deleted so the user
    is not left permanently staring at a misleading 'מעלה לטלגרם... 95%'."""
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=RPCError(None, message="MESSAGE_ID_INVALID"))
    msg.respond = AsyncMock(side_effect=FloodWaitError(None, capture=150))
    msg.delete = AsyncMock()

    reporter = MessageProgressReporter(msg, max_wait_seconds=120.0)
    reporter._max_retries = 1

    await reporter.update(texts.DOWNLOAD_FAILED, is_terminal=True)

    # Respond was attempted and failed
    assert msg.respond.call_count >= 1
    # In M10.5: delete IS called to eliminate misleading stale progress!
    msg.delete.assert_awaited_once()
    assert reporter._message is msg


# -----------------------------------------------------------------------------
# 5. Parallel upload: progress edit flood wait does not timeout or delay upload
# -----------------------------------------------------------------------------
async def test_upload_progress_flood_wait_does_not_timeout_or_delay_upload(tmp_path):
    """Finding 5 / Requirement 5: A prolonged flood wait on progress message edit
    must be skipped and not block the upload lane, preventing REQUEST_TIMEOUT_EXCEEDED."""
    server = PartServer()
    path = make_file(tmp_path / "file.bin", 6 * PART)

    # Reporter whose edits hit 120s flood wait
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=FloodWaitError(None, capture=120))
    reporter = MessageProgressReporter(msg)

    progress = UploadProgress(reporter, "מעלה לטלגרם...", path.stat().st_size)

    # If the upload lane blocked on the 120s flood wait, it would take > 120s.
    # With skipping / bounded progress wait, it finishes within 2 seconds.
    handle = await asyncio.wait_for(
        parallel_upload.upload_file_parallel(server, path, workers=2, progress=progress),
        timeout=5.0,
    )

    # Upload succeeded completely
    assert b"".join(server.parts[handle.id][i] for i in range(6)) == path.read_bytes()


# -----------------------------------------------------------------------------
# 6. event.answer failure does not prevent download start in quality selection
# -----------------------------------------------------------------------------
async def test_quality_pick_event_answer_failure_starts_download_cleanly():
    """Finding 6 / Requirement 6: Failure or flood on event.answer does not crash
    the quality callback handler or prevent the download from starting."""
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    # 1. Post YouTube link to populate quality store
    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    # 2. Simulate callback query with broken event.answer (e.g. QueryIdInvalid / flood)
    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    cb_event.answer = AsyncMock(side_effect=RPCError(None, message="QUERY_ID_INVALID"))

    # Handler must not raise and must continue to start the pipeline
    await ytq_handler(cb_event)

    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 1
    assert kwargs["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert isinstance(kwargs["engine"], YouTubeEngine)
