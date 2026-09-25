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
from pathlib import Path


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

    root.setLevel(level)
    third_party_level = level if level <= logging.DEBUG else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level)
