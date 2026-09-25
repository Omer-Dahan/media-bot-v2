"""A single progress message, edited in place.

The product requirement is one updating message per download, not a stream
of new messages per phase - this wraps whatever message object was sent
first and edits it for every subsequent status change.

A status update is UI polish, not part of the download's success/failure -
by the time the final "done" update runs, the file has already been
delivered and the user already charged (media_bot_v2/pipeline.py). If that
edit fails (message deleted by the user, FloodWait, a transient network
error), the download must still be reported as a success: the failure is
swallowed here rather than left to propagate into the pipeline's except
block, which would otherwise tell the user "download failed" after they
already received and paid for the file.

`ConnectionError`/`TimeoutError`/`OSError` are caught alongside `RPCError`
because they do not inherit from it - a socket hiccup while editing the
message is exactly as harmless to the already-completed transfer as an
`RPCError` and must not be allowed to propagate either.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from telethon.errors import MessageNotModifiedError, RPCError

from media_bot_v2.telegram import texts
from media_bot_v2.telegram.flood_wait import (
    FLOOD_WAIT_ERRORS,
    MAX_FLOOD_WAIT_SECONDS,
    call_with_flood_retry,
    get_flood_wait_seconds,
)

logger = logging.getLogger(__name__)


def _is_terminal_text(text: str, buttons: Any = None, is_terminal: bool | None = None) -> bool:
    if is_terminal is not None:
        return is_terminal
    if buttons is not None:
        return True
    if text.startswith((
        texts.DOWNLOAD_STARTED,
        texts.DOWNLOADING,
        "מוריד",
        "🔄 מוריד",
        texts.PROCESSING,
        "מעבד",
        texts.UPLOADING,
        "מעלה",
        texts.DOWNLOAD_FROM_CACHE,
        "נמצא במטמון",
        texts.YOUTUBE_QUEUE_WAIT,
        "⏳ עומס זמני בשרתי טלגרם",
        texts.PING_MESSAGE,
    )):
        return False
    if "מוריד..." in text or "מעלה לטלגרם..." in text:
        return False
    if text.startswith((texts.DOWNLOAD_DONE, "הושלם")):
        return True
    if text.startswith(("❌", "⏱️")):
        return True
    if "נכשלה" in text or "בוטלה" in text:
        return True
    return text in (
        texts.DOWNLOAD_FAILED,
        texts.FLOOD_WAIT_FAILED,
        texts.CREDITS_EXHAUSTED,
        texts.BANDWIDTH_EXHAUSTED,
        texts.REQUEST_TIMEOUT_EXCEEDED,
        texts.UNSUPPORTED_URL,
    )


class MessageProgressReporter:
    def __init__(
        self,
        message,
        *,
        max_retries: int = 5,
        max_wait_seconds: float = MAX_FLOOD_WAIT_SECONDS,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._message = message
        self._last_text: str | None = None
        self._max_retries = max_retries
        self._max_wait_seconds = max_wait_seconds
        self._sleep = sleep_func
        self._seq: int = 0
        self._last_applied_seq: int = 0
        self._is_terminal_completed: bool = False
        self._async_lock: asyncio.Lock | None = None

    @property
    def _lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    async def handle_flood_wait(self, wait_seconds: int, attempts: int = 1) -> None:
        """When an operation hits a prolonged flood wait (>= 10s), update the progress
        message to inform the user rather than leaving frozen percentages."""
        if wait_seconds >= 10:
            await self.update(texts.FLOOD_WAIT_MESSAGE.format(seconds=wait_seconds), is_terminal=False)

    async def update(self, text: str, *, buttons=None, is_terminal: bool | None = None) -> None:
        """Edit the message to `text`. `buttons` (e.g. the contact button under
        a "credits exhausted" message) is passed only when given, so plain
        status updates keep their exact `edit(text)` call.

        Catches flood waits with automatic retry up to max_retries for terminal messages.
        Non-terminal updates drop long flood waits to avoid blocking the pipeline/upload.
        `_last_text` is only updated on a successful edit. If all edit attempts fail for a
        critical terminal message (finish/error/quota), attempts fallback delivery
        via a new message, updates `self._message` to the new message, and only deletes
        the stale message if fallback succeeded."""
        terminal = _is_terminal_text(text, buttons, is_terminal)
        self._seq += 1
        seq = self._seq

        if not terminal and self._is_terminal_completed:
            logger.debug("Discarding non-terminal progress %r (seq %d): terminal already reached", text, seq)
            return

        if terminal:
            self._is_terminal_completed = True

        if text == self._last_text and buttons is None:
            return

        attempts = 0
        success = False
        sleeper = self._sleep or asyncio.sleep
        while True:
            if not terminal and (self._is_terminal_completed or seq < self._last_applied_seq):
                break

            flood_exc = None
            async with self._lock:
                if not terminal and (self._is_terminal_completed or seq < self._last_applied_seq):
                    break
                try:
                    if buttons is not None:
                        await self._message.edit(text, buttons=buttons)
                    else:
                        await self._message.edit(text)
                    success = True
                    self._last_text = text
                    self._last_applied_seq = seq
                    break
                except MessageNotModifiedError:
                    success = True
                    self._last_text = text
                    self._last_applied_seq = seq
                    break
                except FLOOD_WAIT_ERRORS as exc:
                    flood_exc = exc
                except (RPCError, ConnectionError, TimeoutError, OSError):
                    logger.warning("Failed to edit progress message to %r", text, exc_info=True)
                    break

            if flood_exc is not None:
                attempts += 1
                wait_seconds = get_flood_wait_seconds(flood_exc)
                if not terminal and (attempts > 1 or wait_seconds > 2.0):
                    logger.warning(
                        "Progress non-terminal edit hit flood wait (%s: %ss), skipping",
                        type(flood_exc).__name__,
                        wait_seconds,
                    )
                    break
                if attempts > self._max_retries or wait_seconds > self._max_wait_seconds:
                    logger.warning(
                        "Progress message edit hit flood wait (%s: %ss, attempt %d/%d) exceeding limits",
                        type(flood_exc).__name__,
                        wait_seconds,
                        attempts,
                        self._max_retries,
                    )
                    break
                logger.warning(
                    "Progress message edit hit flood wait (%s: %ss, attempt %d/%d); sleeping %ss before retry",
                    type(flood_exc).__name__,
                    wait_seconds,
                    attempts,
                    self._max_retries,
                    wait_seconds,
                )
                await sleeper(wait_seconds)
                if not terminal and (self._is_terminal_completed or seq < self._last_applied_seq):
                    break
                continue

        if not success and terminal:
            # Terminal messages (finish/failure/quota) are critical:
            # try to deliver via new message and delete stale progress message
            # so the user never stays looking at "מעלה... 95%".
            async with self._lock:
                old_message = self._message
                new_message = None
                if hasattr(old_message, "respond") and callable(old_message.respond):
                    try:
                        if buttons is not None:
                            new_message = await call_with_flood_retry(
                                old_message.respond,
                                text,
                                buttons=buttons,
                                max_retries=self._max_retries,
                                max_wait_seconds=self._max_wait_seconds,
                                sleep_func=self._sleep,
                            )
                        else:
                            new_message = await call_with_flood_retry(
                                old_message.respond,
                                text,
                                max_retries=self._max_retries,
                                max_wait_seconds=self._max_wait_seconds,
                                sleep_func=self._sleep,
                            )
                        if new_message is not None:
                            self._message = new_message
                            self._last_text = text
                            self._last_applied_seq = seq
                            logger.info("Delivered terminal progress message via new message after edit failure: %r", text)
                    except Exception:
                        logger.warning("Failed to deliver fallback terminal message %r", text, exc_info=True)

                if new_message is not None and hasattr(old_message, "delete") and callable(old_message.delete):
                    try:
                        await old_message.delete()
                        logger.info("Deleted stale progress message after fallback delivery")
                    except Exception:
                        logger.debug("Failed to delete stale progress message", exc_info=True)
                elif new_message is None:
                    logger.warning(
                        "Fallback delivery was not successful; retaining previous progress message without deleting: %r",
                        text,
                    )


class UploadProgress:
    """Turns byte counts from the parallel upload lanes into a few progress edits.

    Lanes report concurrently and out of order, so the shown percentage is
    the highest one seen (never goes back, even when a part is retried), is
    capped at 100, and an edit is made only when it moved by `min_step`
    points and `min_interval` seconds passed - at most 100/min_step + 1
    edits per request. Bytes are summed over every part of the request, so a
    split file shows one 0-100% run rather than restarting per part.
    """

    def __init__(
        self,
        reporter,
        label: str,
        total: int,
        *,
        min_step: int = 5,
        min_interval: float = 3.0,
        clock=time.monotonic,
    ) -> None:
        self._reporter = reporter
        self._label = label
        self._total = max(total, 1)
        self._min_step = min_step
        self._min_interval = min_interval
        self._clock = clock
        self._base = 0
        self._shown = 0
        self._shown_at = float("-inf")
        self._in_flood = False

    def start_part(self, bytes_done_before: int) -> None:
        self._base = bytes_done_before

    async def handle_flood_wait(self, wait_seconds: int, attempts: int = 1) -> None:
        """When an upload operation hits a prolonged flood wait (>= 10s),
        update the user with an explanatory Hebrew message rather than freezing."""
        if wait_seconds >= 10:
            self._in_flood = True
            try:
                await self._reporter.update(
                    texts.FLOOD_WAIT_MESSAGE.format(seconds=wait_seconds),
                    is_terminal=False,
                )
            except TypeError:
                await self._reporter.update(texts.FLOOD_WAIT_MESSAGE.format(seconds=wait_seconds))

    async def handle_flood_cleared(self) -> None:
        """When a flood wait finishes, restore the progress message to avoid frozen state."""
        if self._in_flood:
            self._in_flood = False
            text = f"{self._label} {self._shown}%" if self._shown > 0 else self._label
            try:
                await self._reporter.update(text, is_terminal=False)
            except TypeError:
                await self._reporter.update(text)

    async def __call__(self, done: int, _part_total: int) -> None:
        percent = min(100, (self._base + done) * 100 // self._total)
        if percent <= self._shown and not self._in_flood:
            return
        now = self._clock()
        finished = percent == 100
        if (
            not finished
            and not self._in_flood
            and (percent - self._shown < self._min_step or now - self._shown_at < self._min_interval)
        ):
            return
        self._shown = max(self._shown, percent)
        self._shown_at = now
        self._in_flood = False
        try:
            await self._reporter.update(f"{self._label} {percent}%", is_terminal=False)
        except TypeError:
            await self._reporter.update(f"{self._label} {percent}%")
