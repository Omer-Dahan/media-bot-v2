"""Tests for M10.6: Fixing timeout message overwrite, transient RPC error freeze,
three-stage flood delivery, router failure handlers, and quality pick ceiling.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import FloodWaitError, MessageIdInvalidError, RPCError, ServerError

from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress import (
    MessageProgressReporter,
    _is_permanent_edit_error,
)
from tests.test_router import (
    _FakeCallbackEvent,
    _find_url_handler,
    _find_ytq_handler,
    _make_router,
    _QualityEvent,
)


# =============================================================================
# 1. Timeout message is NOT overwritten by DOWNLOAD_FAILED
# =============================================================================
async def test_timeout_message_not_overwritten_in_url_handler():
    """Requirement 1: When pipeline raises TimeoutError after displaying
    REQUEST_TIMEOUT_EXCEEDED, url_handler must not overwrite it with DOWNLOAD_FAILED."""
    pipeline = MagicMock()

    async def fake_pipeline_run(**kwargs):
        progress = kwargs["progress"]
        # Pipeline updates progress to REQUEST_TIMEOUT_EXCEEDED before re-raising TimeoutError
        await progress.update(texts.REQUEST_TIMEOUT_EXCEEDED, is_terminal=True)
        raise TimeoutError("download took too long")

    pipeline.run = AsyncMock(side_effect=fake_pipeline_run)
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)

    # Use a TikTok URL which goes through url_handler pipeline
    event = _QualityEvent("https://www.tiktok.com/@user/video/1234567890", sender_id=42)
    # Give event a mock respond that returns an editable message
    msg = MagicMock()
    msg.edits = []

    async def fake_edit(text, **kwargs):
        msg.edits.append(text)

    msg.edit = AsyncMock(side_effect=fake_edit)
    event.respond = AsyncMock(return_value=msg)

    await url_handler(event)

    # The final message seen by user must be REQUEST_TIMEOUT_EXCEEDED, NOT DOWNLOAD_FAILED
    assert texts.REQUEST_TIMEOUT_EXCEEDED in msg.edits
    assert texts.DOWNLOAD_FAILED not in msg.edits
    assert msg.edits[-1] == texts.REQUEST_TIMEOUT_EXCEEDED


async def test_timeout_message_not_overwritten_in_quality_pick_handler():
    """Requirement 1: When pipeline raises TimeoutError in quality_pick_handler,
    it must not overwrite the timeout message with DOWNLOAD_FAILED."""
    pipeline = MagicMock()

    async def fake_pipeline_run(**kwargs):
        progress = kwargs["progress"]
        await progress.update(texts.REQUEST_TIMEOUT_EXCEEDED, is_terminal=True)
        raise TimeoutError("download took too long")

    pipeline.run = AsyncMock(side_effect=fake_pipeline_run)
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", sender_id=42)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=42)

    await ytq_handler(cb_event)

    # The menu message must have REQUEST_TIMEOUT_EXCEEDED and NOT DOWNLOAD_FAILED
    assert texts.REQUEST_TIMEOUT_EXCEEDED in cb_event.menu_message.edits
    assert texts.DOWNLOAD_FAILED not in cb_event.menu_message.edits
    assert cb_event.menu_message.edits[-1] == texts.REQUEST_TIMEOUT_EXCEEDED


# =============================================================================
# 2. Transient vs permanent RPC errors in progress updates
# =============================================================================
def test_is_permanent_edit_error_classification():
    """Requirement 2: Ensure _is_permanent_edit_error correctly distinguishes
    transient errors (500, RPC_CALL_FAIL) from permanent ones (MESSAGE_ID_INVALID, 400)."""
    # Transient errors
    assert not _is_permanent_edit_error(ServerError(None, message="RPC_CALL_FAIL"))
    assert not _is_permanent_edit_error(RPCError(None, message="RPC_CALL_FAIL"))
    assert not _is_permanent_edit_error(RPCError(None, message="500"))
    assert not _is_permanent_edit_error(RPCError(None, message="Internal Server Error"))
    server_err = RPCError(None, message="Some temporary glitch")
    server_err.code = 503
    assert not _is_permanent_edit_error(server_err)

    # Permanent errors
    assert _is_permanent_edit_error(MessageIdInvalidError(None))
    assert _is_permanent_edit_error(RPCError(None, message="MESSAGE_ID_INVALID"))
    assert _is_permanent_edit_error(RPCError(None, message="CHAT_WRITE_FORBIDDEN"))
    assert _is_permanent_edit_error(RPCError(None, message="MESSAGE_AUTHOR_REQUIRED"))
    bad_req = RPCError(None, message="Bad request")
    bad_req.code = 400
    assert _is_permanent_edit_error(bad_req)


async def test_transient_rpc_error_does_not_freeze_progress():
    """Requirement 2: A transient server error (e.g. 500 / RPC_CALL_FAIL) on one edit
    must NOT mark the message uneditable; subsequent progress edits must continue."""
    msg = MagicMock()
    # First edit fails with ServerError(500), second and third edits succeed
    msg.edit = AsyncMock(side_effect=[ServerError(None, message="RPC_CALL_FAIL"), None, None])

    reporter = MessageProgressReporter(msg)

    # First update hits transient error
    await reporter.update(f"{texts.DOWNLOADING} 10%", is_terminal=False)
    # Reporter should NOT be marked uneditable!
    assert not reporter._uneditable

    # Second update should still call edit (not dropped!)
    await reporter.update(f"{texts.DOWNLOADING} 20%", is_terminal=False)
    assert reporter._last_text == f"{texts.DOWNLOADING} 20%"

    # Third update should also succeed
    await reporter.update(f"{texts.DOWNLOADING} 30%", is_terminal=False)
    assert reporter._last_text == f"{texts.DOWNLOADING} 30%"
    assert msg.edit.call_count == 3


async def test_permanent_rpc_error_marks_uneditable():
    """Requirement 2: A permanent error (e.g. MESSAGE_ID_INVALID) marks message
    uneditable and drops further non-terminal edits."""
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=RPCError(None, message="MESSAGE_ID_INVALID"))

    reporter = MessageProgressReporter(msg)

    await reporter.update(f"{texts.DOWNLOADING} 10%", is_terminal=False)
    assert reporter._uneditable

    # Further non-terminal updates do not attempt to edit
    await reporter.update(f"{texts.DOWNLOADING} 20%", is_terminal=False)
    await reporter.update(f"{texts.DOWNLOADING} 30%", is_terminal=False)
    assert msg.edit.call_count == 1


# =============================================================================
# 3. Three-stage flood handling and failure reporting
# =============================================================================
async def test_three_stage_flood_failure_delivers_error_and_deletes_stale_progress():
    """Requirement 3: When flood wait hits edit, fallback respond, AND delete,
    the stale progress message is not left behind, and a deferred delivery
    ensures the user receives the failure message once the flood clears."""
    msg = MagicMock()
    # Edit hits prolonged flood (3600s)
    msg.edit = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    # Foreground fallback respond hits prolonged flood (3600s), but deferred retry succeeds
    msg.respond = AsyncMock(side_effect=[FloodWaitError(None, capture=3600), MagicMock()])
    # Foreground delete hits prolonged flood (3600s), deferred delete succeeds
    msg.delete = AsyncMock(side_effect=[FloodWaitError(None, capture=3600), None])

    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    reporter = MessageProgressReporter(msg, max_wait_seconds=10.0, sleep_func=fake_sleep)

    # First display 95%
    await reporter.update(f"{texts.UPLOADING} 95%", is_terminal=False)
    # Reset edit mock for terminal update
    msg.edit.side_effect = FloodWaitError(None, capture=3600)

    # Failure terminal update arrives
    await reporter.update(texts.DOWNLOAD_FAILED, is_terminal=True)

    # In foreground: both edit, respond and delete flooded out
    assert reporter._deferred_task is not None

    # Wait for deferred task to complete
    await reporter._deferred_task

    # Once flood clears:
    # 1. User MUST receive failure indication (msg.respond was called with DOWNLOAD_FAILED)
    msg.respond.assert_any_call(texts.DOWNLOAD_FAILED)
    # 2. Stale message must be deleted (msg.delete called again)
    assert msg.delete.call_count >= 2


async def test_three_stage_flood_success_deletes_stale_progress():
    """Requirement 3: When terminal success hits 3-stage flood, stale progress
    message is deleted once flood clears."""
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    msg.respond = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    msg.delete = AsyncMock(side_effect=[FloodWaitError(None, capture=3600), None])

    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    reporter = MessageProgressReporter(msg, max_wait_seconds=10.0, sleep_func=fake_sleep)

    await reporter.update(f"{texts.UPLOADING} 95%", is_terminal=False)
    msg.edit.side_effect = FloodWaitError(None, capture=3600)

    await reporter.update(texts.DOWNLOAD_DONE, is_terminal=True)

    assert reporter._deferred_task is not None
    await reporter._deferred_task

    # Stale progress message was deleted
    assert msg.delete.call_count >= 2


# =============================================================================
# 4. Router routes never left without response on early failure
# =============================================================================
async def test_load_delivery_failure_in_url_handler_responds_with_error():
    """Requirement 4: Database failure in load_delivery in url_handler must
    notify user with generic Hebrew DOWNLOAD_FAILED and not crash silently."""
    client = _make_router()
    url_handler = _find_url_handler(client)

    event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", sender_id=42)
    event.respond = AsyncMock()

    with patch("media_bot_v2.telegram.settings_menu.get_or_create_user", side_effect=RuntimeError("DB pool exhausted")):
        # Must not raise
        await url_handler(event)

    # User received generic DOWNLOAD_FAILED message
    event.respond.assert_awaited_once_with(texts.DOWNLOAD_FAILED)


async def test_load_delivery_failure_in_quality_pick_notifies_user():
    """Requirement 4: Database failure in load_delivery in quality_pick_handler must
    alert user with texts.DOWNLOAD_FAILED and not crash silently."""
    client = _make_router()
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", sender_id=42)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=42)
    cb_event.answer = AsyncMock()

    with patch("media_bot_v2.telegram.settings_menu.get_or_create_user", side_effect=RuntimeError("DB pool exhausted")):
        # Must not raise
        await ytq_handler(cb_event)

    # User received error callback alert and menu message was updated to DOWNLOAD_FAILED
    cb_event.answer.assert_awaited_with(texts.DOWNLOAD_FAILED, alert=True)
    assert texts.DOWNLOAD_FAILED in cb_event.menu_message.edits


async def test_tiktok_engine_construction_failure_updates_progress():
    """Requirement 4: Failure during TikTokEngine construction must update progress
    message to DOWNLOAD_FAILED, not leave it stuck on DOWNLOAD_STARTED."""
    client = _make_router()
    url_handler = _find_url_handler(client)

    event = _QualityEvent("https://www.tiktok.com/@user/video/1234567890", sender_id=42)
    msg = MagicMock()
    msg.edits = []

    async def fake_edit(text, **kwargs):
        msg.edits.append(text)

    msg.edit = AsyncMock(side_effect=fake_edit)
    event.respond = AsyncMock(return_value=msg)

    with patch("media_bot_v2.telegram.router.TikTokEngine", side_effect=RuntimeError("Corrupt cookies")):
        await url_handler(event)

    assert texts.DOWNLOAD_FAILED in msg.edits


async def test_instagram_engine_construction_failure_updates_progress():
    """Requirement 4: Failure during InstagramEngine construction must update progress
    message to DOWNLOAD_FAILED, not leave it stuck on DOWNLOAD_STARTED."""
    client = _make_router()
    url_handler = _find_url_handler(client)

    event = _QualityEvent("https://www.instagram.com/p/C-xyz123/", sender_id=42)
    msg = MagicMock()
    msg.edits = []

    async def fake_edit(text, **kwargs):
        msg.edits.append(text)

    msg.edit = AsyncMock(side_effect=fake_edit)
    event.respond = AsyncMock(return_value=msg)

    with patch("media_bot_v2.telegram.router.InstagramEngine", side_effect=RuntimeError("Invalid session")):
        await url_handler(event)

    assert texts.DOWNLOAD_FAILED in msg.edits


# =============================================================================
# 5. Quality selection total ceiling on status message
# =============================================================================
async def test_quality_pick_status_edit_respects_total_ceiling():
    """Requirement 5: When event.edit and event.respond hit prolonged flood wait (e.g. 60s),
    the handler does not wait 600s; total ceiling aborts waiting quickly and proceeds to download."""
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", sender_id=42)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=42)
    # Prolonged flood of 60s each
    cb_event.edit = AsyncMock(side_effect=FloodWaitError(None, capture=60))
    cb_event.respond = AsyncMock(side_effect=FloodWaitError(None, capture=60))

    start_time = time.monotonic()
    await ytq_handler(cb_event)
    elapsed = time.monotonic() - start_time

    # Since 60s exceeds the ~12s ceiling, it must abort immediately without sleeping 60s or 600s!
    assert elapsed < 2.0
    # And pipeline.run MUST still be called!
    pipeline.run.assert_awaited_once()
