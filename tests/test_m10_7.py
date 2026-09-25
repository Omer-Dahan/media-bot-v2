"""Tests for M10.7: Fixing timeout message regressions, preventing duplicate
messages from deferred retry, resilient deferred flood delivery, and cumulative
flood ceiling in quality pick.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.errors import FloodWaitError

from media_bot_v2.telegram import texts
from media_bot_v2.telegram.flood_wait import call_with_flood_retry
from media_bot_v2.telegram.progress import MessageProgressReporter
from tests.test_router import (
    _FakeCallbackEvent,
    _find_url_handler,
    _find_ytq_handler,
    _make_router,
    _QualityEvent,
)

# =============================================================================
# 1. Single ownership of final message on TimeoutError
# =============================================================================

async def test_foreign_timeout_error_displays_download_failed_in_url_handler():
    """Requirement 1: A foreign TimeoutError (not from budget expiration) must
    display texts.DOWNLOAD_FAILED and MUST NOT be overwritten with
    texts.REQUEST_TIMEOUT_EXCEEDED by url_handler."""
    pipeline = MagicMock()

    async def fake_pipeline_run(**kwargs):
        progress = kwargs["progress"]
        # Pipeline catches foreign TimeoutError and sets DOWNLOAD_FAILED
        await progress.update(texts.DOWNLOAD_FAILED, is_terminal=True)
        raise TimeoutError("internal connection timed out")

    pipeline.run = AsyncMock(side_effect=fake_pipeline_run)
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)

    event = _QualityEvent("https://www.tiktok.com/@user/video/1234567890", sender_id=42)
    msg = MagicMock()
    msg.edits = []

    async def fake_edit(text, **kwargs):
        msg.edits.append(text)

    msg.edit = AsyncMock(side_effect=fake_edit)
    event.respond = AsyncMock(return_value=msg)

    await url_handler(event)

    # Must be DOWNLOAD_FAILED, not REQUEST_TIMEOUT_EXCEEDED
    assert texts.DOWNLOAD_FAILED in msg.edits
    assert texts.REQUEST_TIMEOUT_EXCEEDED not in msg.edits
    assert msg.edits[-1] == texts.DOWNLOAD_FAILED
    # Progress update must have happened exactly once for terminal state
    assert msg.edits.count(texts.DOWNLOAD_FAILED) == 1


async def test_foreign_timeout_error_displays_download_failed_in_quality_pick():
    """Requirement 1: A foreign TimeoutError in YouTube quality pick must display
    DOWNLOAD_FAILED and must not be overwritten with REQUEST_TIMEOUT_EXCEEDED."""
    pipeline = MagicMock()

    async def fake_pipeline_run(**kwargs):
        progress = kwargs["progress"]
        await progress.update(texts.DOWNLOAD_FAILED, is_terminal=True)
        raise TimeoutError("upstream socket timed out")

    pipeline.run = AsyncMock(side_effect=fake_pipeline_run)
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", sender_id=42)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=42)
    await ytq_handler(cb_event)

    assert texts.DOWNLOAD_FAILED in cb_event.menu_message.edits
    assert texts.REQUEST_TIMEOUT_EXCEEDED not in cb_event.menu_message.edits
    assert cb_event.menu_message.edits[-1] == texts.DOWNLOAD_FAILED
    assert cb_event.menu_message.edits.count(texts.DOWNLOAD_FAILED) == 1


async def test_real_budget_timeout_displays_timeout_message_once():
    """Requirement 1: A genuine budget timeout displays REQUEST_TIMEOUT_EXCEEDED
    and router does not send it twice or overwrite it."""
    pipeline = MagicMock()

    async def fake_pipeline_run(**kwargs):
        progress = kwargs["progress"]
        await progress.update(texts.REQUEST_TIMEOUT_EXCEEDED, is_terminal=True)
        raise TimeoutError("budget expired")

    pipeline.run = AsyncMock(side_effect=fake_pipeline_run)
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)

    event = _QualityEvent("https://www.instagram.com/p/C-xyz123/", sender_id=42)
    msg = MagicMock()
    msg.edits = []

    async def fake_edit(text, **kwargs):
        msg.edits.append(text)

    msg.edit = AsyncMock(side_effect=fake_edit)
    event.respond = AsyncMock(return_value=msg)

    await url_handler(event)

    assert texts.REQUEST_TIMEOUT_EXCEEDED in msg.edits
    assert texts.DOWNLOAD_FAILED not in msg.edits
    assert msg.edits[-1] == texts.REQUEST_TIMEOUT_EXCEEDED
    assert msg.edits.count(texts.REQUEST_TIMEOUT_EXCEEDED) == 1


async def test_direct_download_timeout_single_ownership():
    """Requirement 1: Direct engine route preserves pipeline terminal message."""
    pipeline = MagicMock()

    async def fake_pipeline_run(**kwargs):
        progress = kwargs["progress"]
        await progress.update(texts.DOWNLOAD_FAILED, is_terminal=True)
        raise TimeoutError("download stream timeout")

    pipeline.run = AsyncMock(side_effect=fake_pipeline_run)
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)

    event = _QualityEvent("https://example.com/video.mp4", sender_id=42)
    msg = MagicMock()
    msg.edits = []

    async def fake_edit(text, **kwargs):
        msg.edits.append(text)

    msg.edit = AsyncMock(side_effect=fake_edit)
    event.respond = AsyncMock(return_value=msg)

    await url_handler(event)

    assert texts.DOWNLOAD_FAILED in msg.edits
    assert texts.REQUEST_TIMEOUT_EXCEEDED not in msg.edits
    assert msg.edits[-1] == texts.DOWNLOAD_FAILED


# =============================================================================
# 2. Duplicate delayed message & resilient deferred retry
# =============================================================================

async def test_flood_with_foreground_delete_success_sends_exactly_one_final_message():
    """Requirement 2: When edit and fallback respond are flooded, but foreground
    delete of stale progress succeeds, deferred retry delivers the failure message once.
    The user receives exactly ONE final failure message."""
    msg = MagicMock()
    # Edit flooded out
    msg.edit = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    # Fallback respond flooded out in foreground (3600s), deferred respond succeeds
    new_final_msg = MagicMock()
    msg.respond = AsyncMock(side_effect=[FloodWaitError(None, capture=3600), new_final_msg])
    # Foreground delete SUCCEEDS immediately
    msg.delete = AsyncMock(return_value=None)

    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    reporter = MessageProgressReporter(msg, max_wait_seconds=10.0, sleep_func=fake_sleep)

    await reporter.update(f"{texts.UPLOADING} 95%", is_terminal=False)
    msg.edit.side_effect = FloodWaitError(None, capture=3600)

    # Failure terminal update arrives
    await reporter.update(texts.DOWNLOAD_FAILED, is_terminal=True)

    # Foreground delete was called once and succeeded
    assert msg.delete.call_count == 1
    # Deferred task was scheduled
    assert reporter._deferred_task is not None

    # Wait for deferred task
    await reporter._deferred_task

    # Result: exactly ONE delivered failure message
    # msg.respond was called once for foreground (flooded) and once for deferred (delivered)
    assert msg.respond.call_count == 2
    msg.respond.assert_called_with(texts.DOWNLOAD_FAILED)
    # Edit was NOT attempted again on the already-deleted message
    assert msg.edit.call_count == 2  # initial 95% + 1 failed attempt in foreground


async def test_deferred_retry_cancelled_when_terminal_already_delivered():
    """Requirement 2: If a terminal message is delivered while deferred retry is
    scheduled, deferred retry must be cancelled and produce 0 extra messages."""
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    msg.respond = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    msg.delete = AsyncMock(return_value=None)

    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)
        # Simulate router or another component delivering a terminal message during sleep
        reporter.mark_terminal_delivered()

    reporter = MessageProgressReporter(msg, max_wait_seconds=10.0, sleep_func=fake_sleep)

    await reporter.update(f"{texts.UPLOADING} 95%", is_terminal=False)
    msg.edit.side_effect = FloodWaitError(None, capture=3600)

    await reporter.update(texts.DOWNLOAD_FAILED, is_terminal=True)

    assert reporter._deferred_task is not None
    await reporter._deferred_task

    # msg.respond was only called in foreground (flooded); deferred task did not send another message!
    assert msg.respond.call_count == 1


async def test_deferred_retry_handles_persistent_flood_and_removes_stale():
    """Requirement 2 (Part B): If flood wait persists into the deferred task,
    the deferred retry handles it, stale 95% progress message is removed,
    and user receives failure indication."""
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    # Foreground respond flooded (3600s). Deferred respond hits short flood (3s), then succeeds!
    msg.respond = AsyncMock(side_effect=[
        FloodWaitError(None, capture=3600),
        FloodWaitError(None, capture=3),
        MagicMock(),
    ])
    # Foreground delete flooded (3600s), deferred delete succeeds
    msg.delete = AsyncMock(side_effect=[
        FloodWaitError(None, capture=3600),
        None,
    ])

    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    reporter = MessageProgressReporter(msg, max_wait_seconds=10.0, sleep_func=fake_sleep)

    await reporter.update(f"{texts.UPLOADING} 95%", is_terminal=False)
    msg.edit.side_effect = FloodWaitError(None, capture=3600)

    await reporter.update(texts.DOWNLOAD_FAILED, is_terminal=True)

    assert reporter._deferred_task is not None
    await reporter._deferred_task

    # 1. Failure indication delivered via msg.respond
    msg.respond.assert_any_call(texts.DOWNLOAD_FAILED)
    # 2. Stale 95% message deleted
    assert msg.delete.call_count >= 2


# =============================================================================
# 3. Operational lesson: cumulative flood ceiling in quality selection
# =============================================================================

async def test_call_with_flood_retry_cumulative_sleep_bounded_by_max_wait():
    """Requirement 3: When repeated short flood waits occur, total cumulative sleep
    is bounded by max_wait_seconds, rather than allowing N * max_wait_seconds."""
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(sec: float):
        sleeps.append(sec)

    async def repeating_short_flood():
        nonlocal calls
        calls += 1
        # Each flood wait is 5 seconds, under max_wait_seconds=12.0
        raise FloodWaitError(None, capture=5)

    with pytest.raises(FloodWaitError):
        await call_with_flood_retry(
            repeating_short_flood,
            max_retries=10,
            max_wait_seconds=12.0,
            max_total_wait_seconds=12.0,
            sleep_func=fake_sleep,
        )

    # 1st attempt: 5s slept (total 5s <= 12s)
    # 2nd attempt: 5s slept (total 10s <= 12s)
    # 3rd attempt: 5s exceeds remaining (10 + 5 = 15 > 12s) -> aborts!
    assert calls == 3
    assert sum(sleeps) <= 12.0
    assert len(sleeps) == 2


async def test_quality_pick_total_delay_strictly_bounded_under_repeated_floods():
    """Requirement 3: In quality pick handler, repeated short flood waits cannot
    accumulate ~70s of delay; the total duration is bounded by ~12s."""
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", sender_id=42)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=42)

    # Edit hits repeated 5s flood waits
    cb_event.edit = AsyncMock(side_effect=FloodWaitError(None, capture=5))
    cb_event.respond = AsyncMock(side_effect=FloodWaitError(None, capture=5))

    current_time = 1000.0

    def fake_monotonic():
        return current_time

    sleeps: list[float] = []

    async def fake_sleep(sec: float):
        nonlocal current_time
        sleeps.append(sec)
        current_time += sec

    with patch("time.monotonic", fake_monotonic), patch("asyncio.sleep", fake_sleep):
        await ytq_handler(cb_event)

    # Total sleep during quality pick status transition must be <= 12s (well under ~70s)
    assert sum(sleeps) <= 12.0
    assert len(sleeps) == 2
    # Pipeline still proceeds!
    pipeline.run.assert_awaited_once()


# =============================================================================
# 4. M10.8: the cumulative ceiling is an explicit opt-in, never a global default
# =============================================================================

@pytest.mark.parametrize("floods", [[50, 50, 50], [100, 30]])
async def test_send_file_survives_cumulative_floods_and_delivers(tmp_path, monkeypatch, floods):
    """Media delivery prefers waiting over failing: several floods whose total
    exceeds MAX_FLOOD_WAIT_SECONDS must still end with the file delivered."""
    from telethon.errors import FloodWaitError as _Flood

    from media_bot_v2.telegram.uploader import TelethonUploader

    path = tmp_path / "vid.mp4"
    path.write_bytes(b"x" * 1024)
    pending = list(floods)
    delivered = MagicMock(id=7)

    async def send_file(*args, **kwargs):
        if pending:
            raise _Flood(None, capture=pending.pop(0))
        return delivered

    client = MagicMock()
    client.send_file = AsyncMock(side_effect=send_file)
    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    uploader = TelethonUploader(client, chat_id=1, archive_channel=None)
    result = await uploader.send_file(path)

    assert result is delivered
    assert slept == floods
    assert sum(slept) > 120


async def test_call_with_flood_retry_has_no_cumulative_ceiling_by_default():
    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    remaining = [50, 50, 50]

    async def flaky():
        if remaining:
            raise FloodWaitError(None, capture=remaining.pop(0))
        return "ok"

    assert await call_with_flood_retry(flaky, sleep_func=fake_sleep) == "ok"
    assert sum(slept) == 150


async def test_call_with_flood_retry_explicit_total_ceiling_still_enforced():
    async def fake_sleep(sec: float):
        pass

    async def always():
        raise FloodWaitError(None, capture=5)

    with pytest.raises(FloodWaitError):
        await call_with_flood_retry(
            always, max_retries=10, max_total_wait_seconds=12.0, sleep_func=fake_sleep
        )
