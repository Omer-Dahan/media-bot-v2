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
