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
enforces the cap itself, so the user always gets the detected size and the
configured limit.

An exact `total_bytes` (a real `Content-Length`) and `downloaded_bytes` (real
traffic) are both trusted immediately and reject on their own. `total_bytes_estimate`
is not: for a split HLS/DASH format yt-dlp extrapolates it from the first
fragments alone, and that extrapolation can land far above the real eventual
size (production, 11.10: a video refused as "4.7GB" against a 4GiB cap, on a
file that likely would have fit). A merely-over-cap estimate is therefore not
evidence by itself - only a GROSSLY over-cap estimate is, once a small floor
of real traffic backs it up. See `DownloadGuard.check` for the exact rule and
the trade-off it accepts.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yt_dlp

from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError
from media_bot_v2.engines.safe_filename import build_safe_named_file
from media_bot_v2.telegram import texts

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

TRANSPORT_RETRIES = 3

_PARTIAL_NAME_RE = re.compile(r"(\.part(-Frag\d+)?|\.ytdl|\.temp)$", re.IGNORECASE)

# yt-dlp also reports its progress through this same interface
# (`to_screen` -> `logger.debug`), not just real diagnostics - these must
# never be promoted to WARNING even if a future yt-dlp version ever routes
# them through `warning`/`error` instead.
_YTDLP_PROGRESS_SPAM_RE = re.compile(r"^\[download\]")


class YtdlpLoggerAdapter:
    """yt-dlp's `logger` option (https://github.com/yt-dlp/yt-dlp - see
    `YoutubeDL.to_screen`/`report_warning`/`report_error`): forwards yt-dlp's
    own `ERROR:`/`WARNING:` and debug lines into
    `logging.getLogger("media_bot_v2.ytdlp")` instead of letting yt-dlp print
    them straight to stderr, which is what happens when `quiet=True` and no
    `logger` is passed. That is exactly the text an operator needs when a
    download fails (nsig, PO token, HTTP 403/...) - without this adapter it
    never reaches the per-request buffer (`logging_setup.RequestLogHandler`)
    or the file/journal.

    `error`/`warning` land at WARNING with no traceback (yt-dlp already
    folds the relevant context into the message itself); `debug`/`info` land
    at DEBUG. Progress spam (`[download]  42.1%`, `[download] Destination:
    ...`) is routed to DEBUG regardless of which method it comes through, so
    it can never flood the file/journal handlers (configured at INFO) or an
    operator's WARNING-and-above view.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("media_bot_v2.ytdlp")

    def debug(self, msg: str) -> None:
        self._logger.debug(msg)

    def info(self, msg: str) -> None:
        self._logger.debug(msg)

    def warning(self, msg: str) -> None:
        if _YTDLP_PROGRESS_SPAM_RE.match(msg):
            self._logger.debug(msg)
            return
        self._logger.warning(msg)

    def error(self, msg: str) -> None:
        if _YTDLP_PROGRESS_SPAM_RE.match(msg):
            self._logger.debug(msg)
            return
        self._logger.warning(msg)


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
    """Size cap + cancellation, evaluated from yt-dlp hooks.

    `total_bytes_estimate` alone must never reject a merely-over-cap download
    (M12.5a): it is yt-dlp's extrapolation from the first fragments of a
    split HLS/DASH format and can land far above the real eventual size
    (production, 11.10: a 4.7GB estimate against a 4GiB cap, on a video that
    likely would have fit under it). An estimate rejects on its own only
    when it is GROSSLY over the cap - `estimate > max_size * _GROSS_ESTIMATE_FACTOR`
    - and only once `_ESTIMATE_MIN_REAL_BYTES` of real traffic backs it up,
    so a first-fragment spike alone cannot trigger it. An estimate that is
    over the cap but inside that gross band does not reject by itself: the
    download proceeds, and `downloaded_bytes` crossing the cap below still
    stops a genuinely oversize file. Trade-off accepted on purpose: at most
    one cap's worth of real bandwidth/disk can be spent on a file whose
    estimate turns out to have been wrong.
    """

    _GROSS_ESTIMATE_FACTOR = 2.0  # estimate must clear 2x the cap to reject alone
    _ESTIMATE_MIN_REAL_BYTES = 1 * 1024 * 1024  # ~1MiB: ignore a first-fragment spike

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
        total_bytes = d.get("total_bytes") or 0
        received = d.get("downloaded_bytes") or 0
        estimate = d.get("total_bytes_estimate") or 0

        if total_bytes > self._max_size:
            detected = total_bytes
        elif received > self._max_size:
            detected = received
        elif (
            estimate > self._max_size * self._GROSS_ESTIMATE_FACTOR
            and received >= self._ESTIMATE_MIN_REAL_BYTES
        ):
            detected = estimate
        else:
            return

        signal = DownloadTooLargeSignal(detected, self._max_size)
        self.oversize.append(signal)
        raise signal

    def match_filter(self) -> Callable[..., None]:
        """A yt-dlp `match_filter` that only exists to stop a cancelled
        playlist before it extracts/starts its next entry."""

        def _filter(info_dict, *, incomplete: bool = False):
            self.check_cancelled()

        return _filter


def safe_outtmpl(dest_dir: Path) -> str:
    """yt-dlp output template that is always filesystem-safe to write,
    whatever the video's title contains (finding D).

    The stock `%(title).150s [%(id)s].%(ext)s` template truncates by
    *character* count, not bytes: a long Hebrew or emoji-heavy title can
    still blow past the 255-byte NAME_MAX and make yt-dlp fail the download
    with `OSError: [Errno 36] File name too long` before a single byte is
    written. `id` alone is always short and filesystem-safe, so the
    download itself can never fail on the name - `rename_to_safe_display_name`
    below renames the finished file to a human-readable, still byte-capped
    name afterwards, for delivery.
    """
    return str(dest_dir / "%(id)s.%(ext)s")


def rename_to_safe_display_name(entry: dict, path_str: str) -> str:
    """Rename a file yt-dlp wrote under `safe_outtmpl`'s id-based name to a
    human-readable `<title> [<id>]<tail>` name, with the title truncated to
    a safe UTF-8 byte budget (finding D) - never the id or the real
    extension/subtitle-language tail, which come after the id verbatim in
    whatever yt-dlp originally produced."""
    path = Path(path_str)
    video_id = str(entry.get("id") or "")
    if not video_id or not path.name.startswith(video_id):
        return path_str  # unexpected shape - leave untouched rather than guess
    tail = path.name[len(video_id):]  # e.g. ".mp4" or ".he.srt"
    title = entry.get("title") or "video"
    new_name = build_safe_named_file(title, suffix=f" [{video_id}]{tail}")
    if new_name == path.name:
        return path_str
    new_path = path.with_name(new_name)
    try:
        path.rename(new_path)
    except OSError:
        logger.warning("Could not rename %s to safe display name %s", path, new_path)
        return path_str
    return str(new_path)


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
