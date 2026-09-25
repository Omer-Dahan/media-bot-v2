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

import logging
import time

from telethon.errors import MessageNotModifiedError, RPCError

logger = logging.getLogger(__name__)


class MessageProgressReporter:
    def __init__(self, message) -> None:
        self._message = message
        self._last_text: str | None = None

    async def update(self, text: str, *, buttons=None) -> None:
        """Edit the message to `text`. `buttons` (e.g. the contact button under
        a "credits exhausted" message) is passed only when given, so plain
        status updates keep their exact `edit(text)` call."""
        if text == self._last_text and buttons is None:
            return
        self._last_text = text
        try:
            if buttons is not None:
                await self._message.edit(text, buttons=buttons)
            else:
                await self._message.edit(text)
        except MessageNotModifiedError:
            pass  # content unchanged from Telegram's point of view - nothing to surface
        except (RPCError, ConnectionError, TimeoutError, OSError):
            logger.warning("Failed to edit progress message to %r", text, exc_info=True)


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

    def start_part(self, bytes_done_before: int) -> None:
        self._base = bytes_done_before

    async def __call__(self, done: int, _part_total: int) -> None:
        percent = min(100, (self._base + done) * 100 // self._total)
        if percent <= self._shown:
            return
        now = self._clock()
        finished = percent == 100
        if not finished and (percent - self._shown < self._min_step or now - self._shown_at < self._min_interval):
            return
        self._shown = percent
        self._shown_at = now
        await self._reporter.update(f"{self._label} {percent}%")
