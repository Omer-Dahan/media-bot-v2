"""Tests for M10.5: Critical except syntax fix, double flood handling, and router resilience."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import FloodWaitError, RPCError

from media_bot_v2.telegram import parallel_upload, texts
from media_bot_v2.telegram.progress import (
    MessageProgressReporter,
    UploadProgress,
)
from media_bot_v2.telegram.router import _safe_answer_callback
from tests.test_multi_connection_upload import ConnectionNetwork
from tests.test_parallel_upload import PART, make_file
from tests.test_router import (
    _FakeCallbackEvent,
    _find_url_handler,
    _find_ytq_handler,
    _make_router,
    _QualityEvent,
)


# -----------------------------------------------------------------------------
# 1. AST check: No except clause in codebase contains a nested tuple
# -----------------------------------------------------------------------------
def test_no_nested_tuples_in_except_clauses():
    """Requirement 1: Verify via AST that no except clause in the codebase
    catches a nested tuple, which would raise TypeError at runtime."""
    root_dir = Path(__file__).resolve().parent.parent
    py_files = list(root_dir.glob("media_bot_v2/**/*.py")) + list(root_dir.glob("tests/**/*.py"))
    assert py_files, "No python files found to inspect"

    for file_path in py_files:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type and isinstance(node.type, ast.Tuple):
                for elt in node.type.elts:
                        # Direct nested tuple or list is forbidden
                        assert not isinstance(elt, (ast.Tuple, ast.List)), (
                            f"Nested tuple/list in except handler at {file_path}:{node.lineno}"
                        )
                        # Check that known tuple constants are unpacked (ast.Starred)
                        if isinstance(elt, ast.Name) and elt.id == "FLOOD_WAIT_ERRORS":
                            pytest.fail(
                                f"FLOOD_WAIT_ERRORS is a tuple and must be unpacked with * inside except tuple at {file_path}:{node.lineno}"
                            )


# -----------------------------------------------------------------------------
# 1b. Parallel upload succeeds with slow progress edit (>1s) and short flood (1-2s)
# -----------------------------------------------------------------------------
async def test_parallel_upload_succeeds_with_slow_edit_and_short_flood(tmp_path, monkeypatch):
    """Requirement 1: Parallel upload with workers=2 and connections=2 must succeed
    when progress edit is slow (>1s timeout) or encounters short flood waits (1-2s).
    Resulting file must be byte-for-byte identical to the original."""
    server = ConnectionNetwork()

    async def opener(client):
        return await server.open_sender(client)

    monkeypatch.setattr(parallel_upload, "_open_sender", opener)
    path = make_file(tmp_path / "upload_test.bin", 6 * PART)

    call_count = 0

    async def slow_and_flooding_progress(done: int, total: int):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Slow edit (> 1.0s timeout in parallel_upload._lane)
            await asyncio.sleep(1.2)
        elif call_count == 2:
            # Short flood wait (1 second)
            raise FloodWaitError(None, capture=1)

    # upload_file_parallel with 2 workers and 2 connections
    handle = await parallel_upload.upload_file_parallel(
        server,
        path,
        workers=2,
        connections=2,
        progress=slow_and_flooding_progress,
    )

    # Must complete cleanly and reconstructed data must be byte-for-byte identical
    uploaded_data = b"".join(server.parts[handle.id][i] for i in range(6))
    assert uploaded_data == path.read_bytes()
    assert call_count >= 2
    # Multi-connection verification: both real connections must have carried parts
    assert len(server.used_connections) == 2
    assert server._sender in server.used_connections
    assert server.opens == 1
    assert all(s.disconnected for s in server.senders)


# -----------------------------------------------------------------------------
# 2. Double flood on terminal message: stale progress message is deleted
# -----------------------------------------------------------------------------
async def test_double_flood_terminal_failure_deletes_stale_progress_message(caplog):
    """Requirement 2: When both edit and fallback respond hit prolonged flood waits
    (e.g. 3600s), the stale progress message (e.g. 95%) is deleted to prevent misleading status,
    and a clear error is logged."""
    msg = MagicMock()
    # First edit (95%) succeeds; second edit (terminal) hits prolonged flood (3600s)
    msg.edit = AsyncMock(side_effect=[None, FloodWaitError(None, capture=3600)])
    # Fallback respond also hits prolonged flood (3600s)
    msg.respond = AsyncMock(side_effect=FloodWaitError(None, capture=3600))
    msg.delete = AsyncMock()

    reporter = MessageProgressReporter(msg, max_wait_seconds=120.0)

    # First simulate an in-progress update that successfully displayed 95%
    await reporter.update(f"{texts.UPLOADING} 95%", is_terminal=False)
    assert reporter._last_text == f"{texts.UPLOADING} 95%"

    # Terminal message arrives, but both edit and fallback flood out
    with caplog.at_level("ERROR"):
        await reporter.update(texts.DOWNLOAD_DONE, is_terminal=True)

    # Stale progress message was deleted
    msg.delete.assert_awaited_once()
    # Logged clearly as error
    assert any("Terminal message" in record.message for record in caplog.records)


# -----------------------------------------------------------------------------
# 3. Message deleted by user: non-terminal updates stopped, no traceback flood
# -----------------------------------------------------------------------------
async def test_deleted_message_stops_non_terminal_api_calls_and_no_traceback_flood(caplog):
    """Requirement 3: After message is deleted (RPCError on edit), further non-terminal
    progress updates do not call API, no tracebacks are logged, and terminal update
    delivers via fallback."""
    msg1 = MagicMock()
    msg1.edit = AsyncMock(side_effect=RPCError(None, message="MESSAGE_ID_INVALID"))
    msg1.delete = AsyncMock()

    msg2 = MagicMock()
    msg2.edit = AsyncMock()
    msg2.delete = AsyncMock()
    msg1.respond = AsyncMock(return_value=msg2)

    reporter = MessageProgressReporter(msg1)

    # Send 10 non-terminal updates
    with caplog.at_level("WARNING"):
        for pct in range(10, 100, 10):
            await reporter.update(f"{texts.DOWNLOADING} {pct}%", is_terminal=False)

    # Exactly 1 call to edit was made (to detect uneditable), remaining 8 were skipped!
    assert msg1.edit.call_count == 1
    # Check that warning was logged once without traceback spam (exc_info=False)
    warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warn_records) == 1
    assert warn_records[0].exc_info is None

    # Final terminal update delivers via respond fallback
    await reporter.update(texts.DOWNLOAD_DONE, is_terminal=True)
    msg1.respond.assert_awaited_once_with(texts.DOWNLOAD_DONE)
    assert reporter._message is msg2


# -----------------------------------------------------------------------------
# 4. _safe_answer_callback: total time ceiling
# -----------------------------------------------------------------------------
async def test_safe_answer_callback_total_time_ceiling():
    """Requirement 4: Repeated flood waits (e.g. 8s each) do not exceed the total
    max_wait_seconds ceiling (10s), and the function proceeds immediately."""
    event = MagicMock()
    # Repeated 8-second flood waits
    event.answer = AsyncMock(side_effect=FloodWaitError(None, capture=8))

    slept: list[float] = []

    async def fake_sleep(sec: float):
        slept.append(sec)

    # Run _safe_answer_callback with 10.0s total ceiling
    await _safe_answer_callback(event, "toast", max_wait_seconds=10.0, sleep_func=fake_sleep)

    # First attempt slept 8s. Second attempt had only 2s remaining, so 8s > 2s broke out.
    assert sum(slept) <= 10.0
    assert len(slept) == 1
    assert slept[0] == 8.0
    assert event.answer.call_count == 2


# -----------------------------------------------------------------------------
# 5. Quality selection: double failure on edit and respond does not crash
# -----------------------------------------------------------------------------
async def test_quality_pick_double_failure_starts_download_anyway():
    """Requirement 5: If both event.edit and event.respond fail (flood / network),
    the handler does not crash and pipeline.run is still called."""
    pipeline = AsyncMock()
    client = _make_router(pipeline=pipeline)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)
    # Both edit and respond fail with FloodWaitError(300)
    cb_event.edit = AsyncMock(side_effect=FloodWaitError(None, capture=300))
    cb_event.respond = AsyncMock(side_effect=FloodWaitError(None, capture=300))

    # ytq_handler must not raise
    await ytq_handler(cb_event)

    # pipeline.run MUST still be called!
    pipeline.run.assert_awaited_once()
    _, kwargs = pipeline.run.call_args
    assert kwargs["user_id"] == 1
    assert kwargs["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# -----------------------------------------------------------------------------
# 6. Exception before pipeline.run notifies user with DOWNLOAD_FAILED
# -----------------------------------------------------------------------------
async def test_exception_before_pipeline_run_notifies_user():
    """Requirement 6: Exception in router before pipeline.run (e.g. DB error in credits_service)
    sends a generic Hebrew error message (texts.DOWNLOAD_FAILED) and logs details."""
    pipeline = AsyncMock()
    credits_service = MagicMock()
    # Simulate DB / internal failure in get_total_credits
    credits_service.get_total_credits.side_effect = RuntimeError("DB connection pool exhausted")

    client = _make_router(pipeline=pipeline, credits_service=credits_service)
    url_handler = _find_url_handler(client)
    ytq_handler = _find_ytq_handler(client)

    url_event = _QualityEvent("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 1)
    await url_handler(url_event)
    button_data = url_event.buttons[0][0].type.data

    cb_event = _FakeCallbackEvent(button_data, sender_id=1)

    await ytq_handler(cb_event)

    # pipeline.run was not reached due to DB error
    pipeline.run.assert_not_called()
    # But user was NOT left stranded: DOWNLOAD_FAILED was sent!
    assert texts.DOWNLOAD_FAILED in cb_event.menu_message.edits


# -----------------------------------------------------------------------------
# 7. UploadProgress._shown is updated only after await succeeds
# -----------------------------------------------------------------------------
async def test_upload_progress_cancellation_does_not_lose_percentage():
    """Requirement 7: If progress update at 100% is cancelled or fails,
    _shown is not advanced before the await, allowing subsequent attempt to show it."""
    reporter = MagicMock()
    cancel_on_first = True

    async def flaky_update(text, **kwargs):
        nonlocal cancel_on_first
        if cancel_on_first:
            cancel_on_first = False
            raise asyncio.CancelledError()

    reporter.update = flaky_update

    progress = UploadProgress(reporter, "מעלה לטלגרם...", total=1000, min_step=1, min_interval=0)

    # First attempt at 100% is cancelled
    with pytest.raises(asyncio.CancelledError):
        await progress(1000, 1000)

    # _shown must still be 0 because the update was cancelled!
    assert progress._shown == 0

    # Second attempt succeeds and updates _shown
    await progress(1000, 1000)
    assert progress._shown == 100
