"""Engine interface placeholder.

Real engines (YouTube/TikTok/Instagram/direct) are implemented starting at
milestone M2 - see spec/SPEC.md. This file only defines the shape so the
router and tests have something concrete to import in M1.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from media_bot_v2.telegram import texts

logger = logging.getLogger(__name__)


class CancellationToken(threading.Event):
    """Thread-safe cancellation token with callback registration for cooperative aborts."""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.set()

    def set(self) -> None:
        super().set()
        with self._lock:
            cbs = list(self._callbacks)
            self._callbacks.clear()
        for cb in cbs:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                logger.debug("CancellationToken callback failed: %s", exc)

    def on_cancel(self, cb: Callable[[], None]) -> None:
        already_set = False
        with self._lock:
            if self.is_set():
                already_set = True
            else:
                self._callbacks.append(cb)
        if already_set:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                logger.debug("CancellationToken callback failed: %s", exc)


class DownloadTooLargeError(Exception):
    """Raised when a download exceeds the engine's configured max size."""

    def __init__(
        self,
        message: str | None = None,
        *,
        file_size: float | None = None,
        max_size: float | None = None,
        url: str | None = None,
    ) -> None:
        self.file_size = file_size
        self.max_size = max_size
        self.url = url
        if file_size is not None and max_size is not None:
            super().__init__(texts.format_download_too_large(file_size, max_size))
        elif message is not None:
            super().__init__(message)
        else:
            super().__init__("הקובץ חורג ממגבלת הגודל המרבית.")


class UnsupportedUrlError(Exception):
    """Raised when a URL is not supported by any engine or provider."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or texts.UNSUPPORTED_URL)


class NotMediaContentError(UnsupportedUrlError):
    """Raised when a response is an HTML/JSON/XML/plain-text body rather than media."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or texts.NOT_MEDIA_CONTENT)


@dataclass
class RouteAttempt:
    route_name: str
    reason: str


class RouteAttemptTracker:
    """Tracks attempted providers and engines for user-facing failure summaries."""

    def __init__(self) -> None:
        self.attempts: list[RouteAttempt] = []

    def record(self, route_name: str, reason: str) -> None:
        self.attempts.append(RouteAttempt(route_name=route_name, reason=reason))

    def format_summary(self) -> str:
        return texts.format_failure_summary([(a.route_name, a.reason) for a in self.attempts])


@dataclass
class DownloadResult:
    file_paths: list[str]
    title: str | None = None
    description: str | None = None
    playlist_total: int | None = None
    playlist_downloaded: int | None = None
    playlist_trimmed_reason: str | None = None


class BaseEngine(ABC):
    """Shared contract every platform engine must implement."""

    name: str = "base"
    supported_platforms: tuple[str, ...] = ()

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True if this engine should handle the given URL."""

    @abstractmethod
    async def download(
        self,
        url: str,
        *,
        dest_dir: Path,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        """Download the media into dest_dir, return local file paths + metadata."""
