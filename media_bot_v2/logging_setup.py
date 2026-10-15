"""Structured logging to a rotating file and (optionally) stdout, matching
the old bot's LOG_FILE / LOG_MAX_BYTES / LOG_BACKUP_COUNT env vars.

stdout is what systemd captures into the journal (`journalctl -u
download-bot-v2`), so the console handler uses the same JSON format as the
file and never emits ANSI colour codes.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from media_bot_v2.providers.base import mask_secrets


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Marks handlers installed by configure_logging so a repeat call can replace
# them instead of stacking duplicates.
_OWNED_ATTR = "_media_bot_owned"

# Chatty network libraries: WARNING+ unless the bot itself runs at DEBUG.
_NOISY_LOGGERS = ("telethon", "httpx", "httpcore", "urllib3")

# Bound on a single buffered line - a runaway log call (a dumped payload, a
# huge stack trace argument) must not blow up the per-request buffer's memory
# footprint beyond REQUEST_LOG_MAX_LINES * this constant.
REQUEST_LOG_LINE_MAX_CHARS = 1000

DEFAULT_REQUEST_LOG_MAX_LINES = 300


class RequestLogBuffer:
    """Bounded, append-only log for one in-flight request.

    Every line is masked with `mask_secrets` and capped at
    `REQUEST_LOG_LINE_MAX_CHARS` before being kept, and the buffer itself
    never holds more than `max_lines` entries (the oldest are dropped) - a
    worst case of `max_lines * REQUEST_LOG_LINE_MAX_CHARS` bytes per request,
    never written to disk.
    """

    def __init__(self, *, max_lines: int = DEFAULT_REQUEST_LOG_MAX_LINES) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)

    def append(self, line: str) -> None:
        masked = mask_secrets(line)
        if len(masked) > REQUEST_LOG_LINE_MAX_CHARS:
            masked = masked[:REQUEST_LOG_LINE_MAX_CHARS]
        self._lines.append(masked)

    def lines(self) -> list[str]:
        return list(self._lines)

    def text(self) -> str:
        return "\n".join(self._lines)


_request_log: ContextVar[RequestLogBuffer | None] = ContextVar("_request_log", default=None)


class _RequestLogFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="[%(asctime)s %(levelname)s %(name)s] %(message)s", datefmt="%H:%M:%S")


class RequestLogHandler(logging.Handler):
    """Appends every log record to whichever `RequestLogBuffer` is active on
    the current asyncio task/thread (via `_request_log`), and is a no-op
    outside of a `request_log_scope()`. Must never raise - a formatting
    failure here would otherwise take down the log call site that triggered
    it."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(_RequestLogFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        buffer = _request_log.get()
        if buffer is None:
            return
        try:
            buffer.append(self.format(record))
        except Exception:  # noqa: BLE001, S110 - a logging handler must never raise
            pass


@contextmanager
def request_log_scope(
    *,
    user_id: int | None = None,
    url: str | None = None,
    platform: str | None = None,
    max_lines: int = DEFAULT_REQUEST_LOG_MAX_LINES,
) -> Iterator[RequestLogBuffer]:
    """Gives every log line written during the `with` block (including ones
    written from worker threads via `executor.run_in_thread`, which copies
    the current `contextvars.Context`) a shared, bounded buffer - so a failed
    request can be reported with the log lines that explain it, instead of
    just the final exception. `user_id`/`url`/`platform` are accepted for
    future diagnostics only; they do not affect buffering."""
    buffer = RequestLogBuffer(max_lines=max_lines)
    token = _request_log.set(buffer)
    try:
        yield buffer
    finally:
        _request_log.reset(token)


def current_request_log() -> str:
    """The active request's buffered log lines, newest last, or "" if no
    `request_log_scope()` is active."""
    buffer = _request_log.get()
    if buffer is None:
        return ""
    return buffer.text()


def _own(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _OWNED_ATTR, True)
    return handler


def _line_buffer_stdout() -> None:
    # No-op when stdout is missing (pythonw, some daemons) or already replaced
    # by an object without reconfigure() (pytest capture, custom streams).
    stream = sys.stdout
    if stream is None or not hasattr(stream, "reconfigure"):
        return
    try:
        stream.reconfigure(line_buffering=True)
    except (ValueError, OSError):
        pass


def configure_logging(
    log_file: str,
    max_bytes: int,
    backup_count: int,
    *,
    level: int = logging.INFO,
    log_to_console: bool = True,
) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _OWNED_ATTR, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = JsonFormatter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root.addHandler(_own(file_handler))

    if log_to_console and sys.stdout is not None:
        _line_buffer_stdout()
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root.addHandler(_own(console_handler))

    root.addHandler(_own(RequestLogHandler()))

    root.setLevel(level)
    third_party_level = level if level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level)
