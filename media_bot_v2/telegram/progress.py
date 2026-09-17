"""A single progress message, edited in place.

The product requirement is one updating message per download, not a stream
of new messages per phase - this wraps whatever message object was sent
first and edits it for every subsequent status change.
"""

from __future__ import annotations


class MessageProgressReporter:
    def __init__(self, message) -> None:
        self._message = message
        self._last_text: str | None = None

    async def update(self, text: str) -> None:
        if text == self._last_text:
            return
        self._last_text = text
        await self._message.edit(text)
