"""MessageProgressReporter: editing the status message is UI polish and must
never fail a download that already succeeded (see progress.py docstring)."""

from telethon.errors import MessageNotModifiedError
from telethon.errors.rpcbaseerrors import RPCError

from media_bot_v2.telegram.progress import MessageProgressReporter


class _Message:
    def __init__(self, *, raises: Exception | None = None):
        self.edits: list[str] = []
        self._raises = raises

    async def edit(self, text: str) -> None:
        self.edits.append(text)
        if self._raises is not None:
            raise self._raises


async def test_update_edits_message_once_per_distinct_text():
    message = _Message()
    reporter = MessageProgressReporter(message)

    await reporter.update("מוריד...")
    await reporter.update("מוריד...")  # same text - no-op, no extra edit call
    await reporter.update("מעלה...")

    assert message.edits == ["מוריד...", "מעלה..."]


async def test_update_swallows_message_not_modified_error():
    message = _Message(raises=MessageNotModifiedError(request=None))
    reporter = MessageProgressReporter(message)

    await reporter.update("הושלם ✅")  # must not raise


async def test_update_swallows_generic_rpc_error_on_final_status():
    """Covers finding 9: a FloodWait/network hiccup while editing the
    completion message must not look like the download itself failed."""
    message = _Message(raises=RPCError(request=None, message="Test error"))
    reporter = MessageProgressReporter(message)

    await reporter.update("הושלם ✅")  # must not raise


async def test_update_swallows_connection_error_on_final_status():
    """ConnectionError/TimeoutError/OSError do not inherit from RPCError - a
    dropped connection while editing the "done" message must not surface as
    a failed download after the file was already sent and charged."""
    message = _Message(raises=ConnectionError("connection reset"))
    reporter = MessageProgressReporter(message)

    await reporter.update("הושלם ✅")  # must not raise


async def test_update_swallows_timeout_error():
    message = _Message(raises=TimeoutError("timed out"))
    reporter = MessageProgressReporter(message)

    await reporter.update("הושלם ✅")  # must not raise


async def test_update_swallows_os_error():
    message = _Message(raises=OSError("network is unreachable"))
    reporter = MessageProgressReporter(message)

    await reporter.update("הושלם ✅")  # must not raise
