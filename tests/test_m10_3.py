"""Tests for M10.3: Progress message flood retry, fallback delivery, long wait explanation, and exposed sites."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodPremiumWaitError, FloodWaitError, MessageNotModifiedError, RPCError

from media_bot_v2.bootstrap import start_client_with_flood_retry
from media_bot_v2.telegram import parallel_upload, texts
from media_bot_v2.telegram.flood_wait import call_with_flood_retry
from media_bot_v2.telegram.progress import MessageProgressReporter, UploadProgress
from tests.test_parallel_upload import PART, PartServer, make_file


# -----------------------------------------------------------------------------
# 1. MessageProgressReporter flood wait retry on completion
# -----------------------------------------------------------------------------
async def test_completion_edit_flood_wait_retries_and_succeeds():
    """Finding 1 / Requirement 1: Flood on completion edit ("הושלם ✅")
    waits according to seconds and retries cleanly so user sees completion."""
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    message = MagicMock()
    message.edit = AsyncMock()
    # Attempt 1: FloodWaitError(2), Attempt 2: success
    message.edit.side_effect = [FloodWaitError(None, capture=2), None]

    reporter = MessageProgressReporter(message, sleep_func=fake_sleep)
    reporter._last_text = "מעלה לטלגרם... 95%"

    await reporter.update(texts.DOWNLOAD_DONE)

    assert sleeps == [2]
    assert message.edit.call_count == 2
    message.edit.assert_called_with(texts.DOWNLOAD_DONE)
    assert reporter._last_text == texts.DOWNLOAD_DONE


async def test_completion_edit_failure_falls_back_to_new_message_and_deletes_stale():
    """Finding 1 / Requirement 1: If edit completely fails for a terminal message,
    it falls back to sending a new message and deletes the stale progress message
    so user is never left with "מעלה... 95%"."""
    message = MagicMock()
    message.edit = AsyncMock(side_effect=RPCError(None, message="MESSAGE_ID_INVALID"))
    message.respond = AsyncMock()
    message.delete = AsyncMock()

    reporter = MessageProgressReporter(message)
    reporter._last_text = "מעלה לטלגרם... 95%"

    await reporter.update(texts.DOWNLOAD_DONE)

    # Verifies fallback response sent terminal message
    message.respond.assert_called_once_with(texts.DOWNLOAD_DONE)
    # Verifies stale message was deleted
    message.delete.assert_called_once()
    assert reporter._last_text == texts.DOWNLOAD_DONE


# -----------------------------------------------------------------------------
# 2. Flood on error and quota messages
# -----------------------------------------------------------------------------
async def test_error_message_flood_wait_retries():
    """Flood on error message edit waits and retries so user receives failure feedback."""
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    message = MagicMock()
    message.edit = AsyncMock(side_effect=[FloodWaitError(None, capture=1), None])

    reporter = MessageProgressReporter(message, sleep_func=fake_sleep)
    await reporter.update(texts.DOWNLOAD_FAILED)

    assert sleeps == [1]
    assert message.edit.call_count == 2
    message.edit.assert_called_with(texts.DOWNLOAD_FAILED)
    assert reporter._last_text == texts.DOWNLOAD_FAILED


async def test_quota_message_with_buttons_flood_wait_retries():
    """Flood on quota message edit (with contact buttons) waits and retries."""
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    buttons = [["contact_btn"]]
    message = MagicMock()
    message.edit = AsyncMock(side_effect=[FloodPremiumWaitError(None, capture=3), None])

    reporter = MessageProgressReporter(message, sleep_func=fake_sleep)
    await reporter.update(texts.CREDITS_EXHAUSTED, buttons=buttons)

    assert sleeps == [3]
    assert message.edit.call_count == 2
    message.edit.assert_called_with(texts.CREDITS_EXHAUSTED, buttons=buttons)
    assert reporter._last_text == texts.CREDITS_EXHAUSTED


async def test_quota_message_fallback_delivery_on_total_edit_failure():
    """If quota message edit fails, fallback delivers message with buttons and deletes stale."""
    buttons = [["contact_btn"]]
    message = MagicMock()
    message.edit = AsyncMock(side_effect=RPCError(None, message="MESSAGE_NOT_MODIFIED"))
    # Make MessageNotModifiedError not hit (use generic RPCError)
    message.edit.side_effect = RPCError(None, message="CHAT_ADMIN_REQUIRED")
    message.respond = AsyncMock()
    message.delete = AsyncMock()

    reporter = MessageProgressReporter(message)
    await reporter.update(texts.CREDITS_EXHAUSTED, buttons=buttons)

    message.respond.assert_called_once_with(texts.CREDITS_EXHAUSTED, buttons=buttons)
    message.delete.assert_called_once()
    assert reporter._last_text == texts.CREDITS_EXHAUSTED


# -----------------------------------------------------------------------------
# 3. Intermediate progress updates
# -----------------------------------------------------------------------------
async def test_intermediate_progress_update_flood_wait_retries_without_chat_spam():
    """Intermediate updates retry on flood wait, but if they fail completely,
    they DO NOT spam the chat with new messages (design principle: 1 updating message)."""
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    message = MagicMock()
    message.edit = AsyncMock(side_effect=[FloodWaitError(None, capture=1), None])
    message.respond = AsyncMock()
    message.delete = AsyncMock()

    reporter = MessageProgressReporter(message, sleep_func=fake_sleep)
    await reporter.update("מעלה לטלגרם... 45%")

    assert sleeps == [1]
    assert message.edit.call_count == 2
    assert message.respond.call_count == 0
    assert reporter._last_text == "מעלה לטלגרם... 45%"

    # Total failure on intermediate update does not call respond/delete
    message.edit.side_effect = RPCError(None, message="NETWORK_ERROR")
    await reporter.update("מעלה לטלגרם... 50%")
    assert message.respond.call_count == 0
    assert message.delete.call_count == 0
    assert reporter._last_text == "מעלה לטלגרם... 45%"  # Not updated to 50%


# -----------------------------------------------------------------------------
# 4. _last_text behavior on failed edit (Bug Verification)
# -----------------------------------------------------------------------------
async def test_last_text_not_updated_on_failed_edit():
    """Bug fix verification: _last_text must NOT be marked as sent if the edit fails.
    A subsequent update attempt with the same text must execute rather than be skipped."""
    message = MagicMock()
    # First call fails
    message.edit = AsyncMock(side_effect=RPCError(None, message="FLOOD_WAIT"))

    reporter = MessageProgressReporter(message)
    reporter._max_retries = 1
    reporter._last_text = "מעלה לטלגרם... 90%"

    await reporter.update("הושלם ✅")

    # The bug was that _last_text was set to "הושלם ✅" before edit, so future retries were blocked
    assert reporter._last_text == "מעלה לטלגרם... 90%"
    assert reporter._last_text != "הושלם ✅"

    # Now the error clears: next call with the SAME text "הושלם ✅" must NOT be skipped
    message.edit = AsyncMock(return_value=None)
    await reporter.update("הושלם ✅")

    message.edit.assert_called_once_with("הושלם ✅")
    assert reporter._last_text == "הושלם ✅"


async def test_message_not_modified_updates_last_text():
    """MessageNotModifiedError means Telegram already has this content, so update is successful."""
    message = MagicMock()
    message.edit = AsyncMock(side_effect=MessageNotModifiedError(None))

    reporter = MessageProgressReporter(message)
    await reporter.update("הושלם ✅")

    assert reporter._last_text == "הושלם ✅"


# -----------------------------------------------------------------------------
# 5. Explanatory message during long flood wait (>= 10s)
# -----------------------------------------------------------------------------
async def test_upload_progress_long_flood_wait_explains_and_restores():
    """Requirement 2: When system waits for a long flood wait (>= 10s),
    user sees Hebrew explanation (texts.FLOOD_WAIT_MESSAGE) instead of frozen percentages,
    and progress is restored once the flood wait clears."""
    reporter = MagicMock()
    reporter.update = AsyncMock()
    reporter._last_text = None

    async def update_tracker(t):
        reporter._last_text = t

    reporter.update.side_effect = update_tracker

    upload_progress = UploadProgress(reporter, "מעלה לטלגרם...", total=1000)
    upload_progress._shown = 45
    reporter._last_text = "מעלה לטלגרם... 45%"

    # Long flood wait (15 seconds)
    await upload_progress.handle_flood_wait(15, 1)

    expected_msg = texts.FLOOD_WAIT_MESSAGE.format(seconds=15)
    reporter.update.assert_called_with(expected_msg)
    assert reporter._last_text == expected_msg

    # Flood wait cleared -> restores percentage
    await upload_progress.handle_flood_cleared()
    reporter.update.assert_called_with("מעלה לטלגרם... 45%")
    assert reporter._last_text == "מעלה לטלגרם... 45%"


async def test_upload_progress_short_flood_wait_does_not_flicker():
    """A short flood wait (< 10s) does not replace the progress percentage to avoid flicker."""
    reporter = MagicMock()
    reporter.update = AsyncMock()

    upload_progress = UploadProgress(reporter, "מעלה לטלגרם...", total=1000)
    upload_progress._shown = 45

    await upload_progress.handle_flood_wait(3, 1)
    reporter.update.assert_not_called()


async def test_parallel_upload_hooks_on_flood_and_cleared(tmp_path, monkeypatch):
    """In parallel upload, a long flood wait calls on_flood and on_flood_cleared."""
    server = PartServer()
    server.flood_premium_plan = {2: 1}  # part 2 floods once with 15s wait
    server.flood_premium_seconds = 15

    waited = []

    async def fake_sleep(sec):
        waited.append(sec)

    monkeypatch.setattr(parallel_upload, "_sleep", fake_sleep)

    flood_notifications = []
    cleared_notifications = []

    async def on_flood(wait_seconds, attempts):
        flood_notifications.append((wait_seconds, attempts))

    async def on_flood_cleared():
        cleared_notifications.append(True)

    path = make_file(tmp_path / "file.bin", 6 * PART)
    await parallel_upload.upload_file_parallel(
        server,
        path,
        workers=2,
        on_flood=on_flood,
        on_flood_cleared=on_flood_cleared,
    )

    assert waited == [15]
    assert flood_notifications == [(15, 1)]
    assert cleared_notifications == [True]


# -----------------------------------------------------------------------------
# 6. User texts contain no internal technical terms
# -----------------------------------------------------------------------------
def test_user_texts_contain_no_technical_terms():
    """Requirement 3: "(Flood Wait)" and internal technical terms must be removed
    from all user-facing texts."""
    assert "(Flood Wait)" not in texts.FLOOD_WAIT_FAILED
    assert "Flood Wait" not in texts.FLOOD_WAIT_FAILED
    assert texts.FLOOD_WAIT_FAILED == "❌ טלגרם הגבילה את הפעילות עקב עומס זמני. אנא נסה שוב מאוחר יותר."

    # Scan all user-facing string constants in texts.py
    for attr in dir(texts):
        if attr.isupper() and isinstance(getattr(texts, attr), str):
            val = getattr(texts, attr)
            assert "(Flood Wait)" not in val, f"Leaked technical term in {attr}"


# -----------------------------------------------------------------------------
# 7. Secondary exposed sites
# -----------------------------------------------------------------------------
def test_bootstrap_start_client_with_flood_retry():
    """Requirement 4: bootstrap client.start is protected against startup flood wait."""
    client = MagicMock()
    calls = 0

    def fake_start(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FloodWaitError(None, capture=5)

    client.start = MagicMock(side_effect=fake_start)
    sleeps = []

    start_client_with_flood_retry(
        client,
        bot_token="token123",
        max_retries=3,
        max_wait_seconds=60.0,
        sleeper=sleeps.append,
    )

    assert calls == 2
    assert sleeps == [5]


def test_bootstrap_start_client_aborts_on_excessive_flood():
    """bootstrap aborts and raises cleanly if flood wait exceeds limit."""
    client = MagicMock()
    client.start = MagicMock(side_effect=FloodWaitError(None, capture=120))
    sleeps = []

    with pytest.raises(FloodWaitError):
        start_client_with_flood_retry(
            client,
            bot_token="token123",
            max_retries=3,
            max_wait_seconds=60.0,
            sleeper=sleeps.append,
        )

    assert sleeps == []


async def test_router_event_get_message_flood_retry():
    """Requirement 4: router.py event.get_message handles flood retry."""
    event = MagicMock()
    calls = 0

    async def fake_get_message():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FloodWaitError(None, capture=2)
        return MagicMock(id=123)

    event.get_message = fake_get_message
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    msg = await call_with_flood_retry(event.get_message, sleep_func=fake_sleep)
    assert calls == 2
    assert sleeps == [2]
    assert msg.id == 123
