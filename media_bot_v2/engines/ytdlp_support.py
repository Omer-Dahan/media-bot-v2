"""Helpers shared by every engine that drives yt-dlp (YouTube, Instagram, TikTok local).

Retry policy (one place, so the engines cannot drift apart again): each route
(the local yt-dlp run, and each fallback provider) is attempted exactly once
at the orchestration level - the engines default to `max_retries=0`. Transient
transport failures are retried *inside* yt-dlp only, via `TRANSPORT_RETRIES`
for both `retries` and `fragment_retries`.

Size policy: yt-dlp's own `max_filesize` is deliberately NOT passed. With a
`Content-Length` it silently skips the file (no error, no hook call, so the
user only ever sees "no media received"), and without one it does not enforce
the cap at all. Instead `DownloadGuard.check` runs from the progress hook and
enforces the cap itself, on both the declared total and the bytes actually
received so far, so the user always gets the detected size and the configured
limit.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yt_dlp

from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError
from media_bot_v2.telegram import texts

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

TRANSPORT_RETRIES = 3

_PARTIAL_NAME_RE = re.compile(r"(\.part(-Frag\d+)?|\.ytdl|\.temp)$", re.IGNORECASE)


class DownloadTooLargeSignal(Exception):
    """Raised from inside a yt-dlp progress hook to abort mid-download once
    the detected size exceeds the configured cap - converted to
    DownloadTooLargeError once it surfaces back in `_download_sync`."""

    def __init__(self, file_size: float | str, max_size: float | None = None) -> None:
        if isinstance(file_size, str):
            match = re.search(r"(\d+)\s*bytes exceeds.*?(\d+)", file_size)
            if match:
                self.file_size = float(match.group(1))
                self.max_size = float(match.group(2))
            else:
                self.file_size = 0.0
                self.max_size = float(max_size) if max_size is not None else 0.0
        else:
            self.file_size = float(file_size)
            self.max_size = float(max_size) if max_size is not None else 0.0
        super().__init__(texts.format_download_too_large(self.file_size, self.max_size))


class DownloadCancelledSignal(yt_dlp.utils.DownloadCancelled):
    """Raised from inside a yt-dlp hook to abort the download when cancelled.

    Subclasses yt-dlp's own `DownloadCancelled` on purpose: that is the one
    exception type yt-dlp refuses to swallow under `ignoreerrors`, so a
    cancelled playlist stops entirely instead of moving on to the next item.
    """


def too_large_error(signal: DownloadTooLargeSignal) -> DownloadTooLargeError:
    return DownloadTooLargeError(
        texts.format_download_too_large(signal.file_size, signal.max_size),
        file_size=signal.file_size,
        max_size=signal.max_size,
    )


class DownloadGuard:
    """Size cap + cancellation, evaluated from yt-dlp hooks."""

    def __init__(self, max_size: int, cancel_token: CancellationToken | None) -> None:
        self._max_size = max_size
        self._cancel_token = cancel_token
        # Every oversize detection is recorded before it is raised: under a
        # playlist's `ignoreerrors` yt-dlp swallows the exception and moves on,
        # so the engine reads this list afterwards to tell "item failed" from
        # "item was too large".
        self.oversize: list[DownloadTooLargeSignal] = []

    def check_cancelled(self) -> None:
        if self._cancel_token is not None and self._cancel_token.is_set():
            raise DownloadCancelledSignal("Download cancelled by timeout budget")

    def check(self, d: dict) -> None:
        self.check_cancelled()
        if d.get("status") != "downloading":
            return
        declared = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        received = d.get("downloaded_bytes") or 0
        detected = max(declared, received)
        if detected > self._max_size:
            signal = DownloadTooLargeSignal(detected, self._max_size)
            self.oversize.append(signal)
            raise signal

    def match_filter(self) -> Callable[..., None]:
        """A yt-dlp `match_filter` that only exists to stop a cancelled
        playlist before it extracts/starts its next entry."""

        def _filter(info_dict, *, incomplete: bool = False):
            self.check_cancelled()

        return _filter


def remove_partial_files(dest_dir: Path) -> None:
    """Delete yt-dlp's leftovers (`.part`, `.part-FragN`, `.ytdl`, `.temp`).

    Called on every exit of a yt-dlp run - success, cancel, too-large, error -
    because an aborted or skipped item leaves them behind, and a playlist that
    partially succeeded leaves the failed items' partials next to the good files.
    """
    if not dest_dir.exists():
        return
    for path in dest_dir.rglob("*"):
        if path.is_file() and _PARTIAL_NAME_RE.search(path.name):
            try:
                path.unlink()
            except OSError:
                logger.warning("Failed to delete partial file %s", path)
