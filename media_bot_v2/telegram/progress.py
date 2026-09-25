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

from telethon.errors import MessageNotModifiedError, RPCError, ServerError

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


def _is_permanent_edit_error(exc: Exception) -> bool:
    """Determine whether an edit RPCError represents a permanent failure
    (message was deleted, bad message ID, or lacking permissions to edit)
    or a transient issue (server errors, 5xx, or temporary RPC hiccups)."""
    if isinstance(exc, ServerError):
        return False
    code = getattr(exc, "code", None)
    if code is not None and code >= 500:
        return False

    msg = str(getattr(exc, "message", "") or "").upper()
    transient_markers = ("500", "502", "503", "504", "RPC_CALL_FAIL", "INTERNAL", "SERVER")
    if any(marker in msg for marker in transient_markers):
        return False

    permanent_markers = (
        "MESSAGE_ID_INVALID",
        "CHAT_WRITE_FORBIDDEN",
        "CHAT_ADMIN_REQUIRED",
        "MESSAGE_AUTHOR_REQUIRED",
        "CHANNEL_PRIVATE",
        "USER_IS_BLOCKED",
        "MSG_ID_INVALID",
    )
    if any(marker in msg for marker in permanent_markers):
        return True

    if code in (400, 403, 404):
        return True

    exc_type = type(exc).__name__
    return any(k in exc_type for k in ("MessageIdInvalid", "Forbidden", "Required", "Private", "Blocked"))


async def _deferred_terminal_retry(
    message: Any,
    text: str,
    buttons: Any,
    wait_seconds: float,
    sleep_func: Callable[[float], Awaitable[None]] | None,
    is_failure: bool,
    needs_delete: bool,
) -> None:
    """Deferred delivery or cleanup after a prolonged flood wait expires.
    Guarantees that on failure the user receives an error indication,
    and stale progress messages (e.g. 95%) are never left behind."""
    sleeper = sleep_func or asyncio.sleep
    try:
        if wait_seconds > 0:
            await sleeper(wait_seconds)
    except Exception:
        logger.debug("Deferred terminal sleep interrupted", exc_info=True)
        return

    delivered = False
    new_msg = None
    if is_failure:
        if not needs_delete and hasattr(message, "edit") and callable(message.edit):
            try:
                if buttons is not None:
                    await message.edit(text, buttons=buttons)
                else:
                    await message.edit(text)
                delivered = True
                logger.info("Deferred failure delivery succeeded via edit: %r", text)
            except Exception:
                logger.debug("Deferred edit failed for failure message", exc_info=True)

        if not delivered and hasattr(message, "respond") and callable(message.respond):
            try:
                if buttons is not None:
                    new_msg = await message.respond(text, buttons=buttons)
                else:
                    new_msg = await message.respond(text)
                delivered = True
                logger.info("Deferred failure delivery succeeded via respond: %r", text)
            except Exception:
                logger.warning("Deferred respond failed for failure message: %r", text, exc_info=True)

    if (needs_delete or new_msg is not None) and hasattr(message, "delete") and callable(message.delete):
        try:
            await message.delete()
            logger.info("Deferred deletion of stale message succeeded")
        except Exception:
            logger.debug("Deferred delete failed", exc_info=True)


class MessageProgressReporter:
    def __init__(
        self,
        message: Any = None,
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
        self._uneditable: bool = message is None
        self._logged_uneditable: bool = False
        self._async_lock: asyncio.Lock | None = None
        self._deferred_task: asyncio.Task[None] | None = None

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
        via a new message, updates `self._message` to the new message, and deletes
        the stale message so the user never stays looking at false progress."""
        terminal = _is_terminal_text(text, buttons, is_terminal)
        self._seq += 1
        seq = self._seq

        if not terminal and self._is_terminal_completed:
            logger.debug("Discarding non-terminal progress %r (seq %d): terminal already reached", text, seq)
            return

        if not terminal and self._uneditable:
            logger.debug("Discarding non-terminal progress %r (seq %d): message is uneditable", text, seq)
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
                    if self._message is not None and hasattr(self._message, "edit") and callable(self._message.edit):
                        if buttons is not None:
                            await self._message.edit(text, buttons=buttons)
                        else:
                            await self._message.edit(text)
                        success = True
                        self._last_text = text
                        self._last_applied_seq = seq
                        self._uneditable = False
                        self._logged_uneditable = False
                        break
                    else:
                        self._uneditable = True
                        break
                except MessageNotModifiedError:
                    success = True
                    self._last_text = text
                    self._last_applied_seq = seq
                    self._uneditable = False
                    self._logged_uneditable = False
                    break
                except FLOOD_WAIT_ERRORS as exc:
                    flood_exc = exc
                except RPCError as exc:
                    if _is_permanent_edit_error(exc):
                        self._uneditable = True
                        if not self._logged_uneditable:
                            self._logged_uneditable = True
                            logger.warning(
                                "Progress message is not editable (%s: %s); dropping further non-terminal edits",
                                type(exc).__name__,
                                exc,
                            )
                    else:
                        logger.debug("Transient RPC error while editing progress message to %r: %s", text, exc)
                    break
                except (ConnectionError, TimeoutError, OSError) as exc:
                    logger.debug("Transient error while editing progress message to %r: %s", text, exc)
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
                respond_exc = None
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
                            self._uneditable = False
                            self._logged_uneditable = False
                            logger.info("Delivered terminal progress message via new message after edit failure: %r", text)
                    except Exception as exc:
                        respond_exc = exc
                        logger.warning("Failed to deliver fallback terminal message %r", text, exc_info=True)

                if new_message is not None and hasattr(old_message, "delete") and callable(old_message.delete):
                    try:
                        await call_with_flood_retry(
                            old_message.delete,
                            max_retries=self._max_retries,
                            max_wait_seconds=self._max_wait_seconds,
                            sleep_func=self._sleep,
                        )
                        logger.info("Deleted stale progress message after fallback delivery")
                    except Exception:
                        logger.debug("Failed to delete stale progress message", exc_info=True)
                elif new_message is None:
                    is_failure = not text.startswith((texts.DOWNLOAD_DONE, "הושלם"))
                    logger.error(
                        "Terminal message %r could not be delivered (both edit and fallback failed); "
                        "deleting stale progress message to prevent misleading status",
                        text,
                    )
                    deleted = False
                    del_exc = None
                    if hasattr(old_message, "delete") and callable(old_message.delete):
                        try:
                            await call_with_flood_retry(
                                old_message.delete,
                                max_retries=self._max_retries,
                                max_wait_seconds=self._max_wait_seconds,
                                sleep_func=self._sleep,
                            )
                            deleted = True
                            logger.info("Deleted stale progress message after complete terminal delivery failure")
                        except Exception as exc:
                            del_exc = exc
                            logger.warning(
                                "Failed to delete stale progress message after terminal delivery failure: %r",
                                text,
                                exc_info=True,
                            )

                    if not deleted or is_failure:
                        flood_secs = [
                            get_flood_wait_seconds(e)
                            for e in (flood_exc, respond_exc, del_exc)
                            if e is not None and isinstance(e, FLOOD_WAIT_ERRORS)
                        ]
                        wait_seconds = float(max(flood_secs)) if flood_secs else 10.0
                        self._deferred_task = asyncio.create_task(
                            _deferred_terminal_retry(
                                old_message,
                                text,
                                buttons,
                                wait_seconds=wait_seconds,
                                sleep_func=self._sleep,
                                is_failure=is_failure,
                                needs_delete=not deleted,
                            )
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
        try:
            await self._reporter.update(f"{self._label} {percent}%", is_terminal=False)
        except TypeError:
            await self._reporter.update(f"{self._label} {percent}%")
        self._shown = max(self._shown, percent)
        self._shown_at = now
        self._in_flood = False
