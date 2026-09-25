"""Instagram download engine backed by yt-dlp with curl-cffi impersonation.

Designed after YouTubeEngine:
- Uses yt-dlp with browser impersonation (curl-cffi) for public content extraction.
- Supports optional cookies via INSTAGRAM_COOKIES_FILE env var or constructor arg,
  defaulting to anonymous extraction without requiring any account/cookies.
- Strict error classification with Hebrew messages for private/login-required,
  not found/deleted, unsupported media/live streams, and network errors.
- Progress reporting and mid-download size cap enforcement via progress hooks.
- One attempt per route (`max_retries=0`); transport retries happen inside yt-dlp only
  (see engines/ytdlp_support.py). Whitelisted Hebrew error messages, clean aborts and
  removal of partial files on every exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.ytdlp_support import (
    TRANSPORT_RETRIES,
    DownloadCancelledSignal,
    DownloadGuard,
    DownloadTooLargeSignal,
    remove_partial_files,
    too_large_error,
)
from media_bot_v2.telegram import texts

logger = logging.getLogger(__name__)

_PROGRESS_THROTTLE_SECONDS = 2.0

_INSTAGRAM_DOMAINS = ("instagram.com", "instagr.am")
_INSTAGRAM_PATH_RE = re.compile(
    r"^/(?:p|reel|reels|tv|share(?:/(?:p|reel|reels))?|stories(?:/highlights)?)/([a-zA-Z0-9_.-]+)",
    re.IGNORECASE,
)


class InstagramDownloadError(Exception):
    """Raised with an already-classified, Hebrew, user-facing message."""


_DownloadTooLargeSignal = DownloadTooLargeSignal
_DownloadCancelledSignal = DownloadCancelledSignal


def matches_instagram_url(url: str) -> bool:
    """Check if the URL belongs to Instagram and has a supported content path.

    Supports instagram.com, instagr.am, and subdomains (www, m), with paths:
    /p/, /reel/, /reels/, /tv/, and share/stories links.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    if not any(host == d or host.endswith("." + d) for d in _INSTAGRAM_DOMAINS):
        return False
    path = parsed.path or ""
    return bool(_INSTAGRAM_PATH_RE.match(path)) or any(
        path.startswith(p) for p in ("/p/", "/reel/", "/reels/", "/tv/", "/share/", "/stories/")
    )


def extract_instagram_id(url: str) -> str | None:
    """Extract a shortcode or identifier from an Instagram URL for stable caching."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if not any(host == d or host.endswith("." + d) for d in _INSTAGRAM_DOMAINS):
        return None
    match = _INSTAGRAM_PATH_RE.match(parsed.path or "")
    if match:
        return match.group(1)
    return None


_ERROR_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "not granting access",
            "empty media response",
            "login required",
            "this account is private",
            "this content is private",
            "private account",
            "requires authentication",
            "checkpoint_required",
            "sign in",
            "confirm you're not a bot",
            "bot detection",
            "user has restricted",
            "restricted",
            "http error 401",
            "http error 403",
        ),
        texts.INSTAGRAM_PRIVATE_OR_LOGIN,
    ),
    (
        (
            "this post has been deleted",
            "post is unavailable",
            "not found",
            "does not exist",
            "has been removed",
            "http error 404",
        ),
        texts.INSTAGRAM_NOT_FOUND,
    ),
    (
        (
            "unsupported url",
            "is not a valid url",
            "unknown url type",
            "url is not supported",
        ),
        texts.UNSUPPORTED_URL,
    ),
    (
        (
            "unsupported media",
            "no video formats found",
            "format not available",
            "requested format is not available",
            "live event",
            "live stream",
        ),
        texts.INSTAGRAM_UNSUPPORTED_MEDIA,
    ),
)

_NETWORK_PATTERNS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "timed out",
    "urlopen error",
    "network is unreachable",
    "no route to host",
    "temporary failure in name resolution",
    "read timed out",
    "connection aborted",
    "remotedisconnected",
    "incompleteread",
)


def classify_instagram_error(message: str | None) -> str:
    """Translate yt-dlp / network error message to a precise Hebrew diagnostic.

    Whitelist only, like `classify_youtube_error`: the user sees a fixed message
    and never any part of `message` (paths, hosts, IPs, tokens); the raw text
    is logged instead.
    """
    if not message:
        return "ההורדה נכשלה: לא התקבל קובץ מדיה מאינסטגרם."
    lowered = message.lower()
    for keywords, hebrew in _ERROR_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            return hebrew
    if any(pattern in lowered for pattern in _NETWORK_PATTERNS):
        return texts.INSTAGRAM_NETWORK_ERROR
    logger.warning("Unclassified Instagram error (details withheld from user): %s", message)
    return texts.INSTAGRAM_GENERIC_FAILURE


def is_retryable_error(message: str | None) -> bool:
    """Only network dropouts and 5xx server issues qualify for retry."""
    if not message:
        return False
    lowered = message.lower()
    return any(pattern in lowered for pattern in _NETWORK_PATTERNS) or "http error 5" in lowered


def _human_size(num_bytes: float | None) -> str:
    if not num_bytes:
        return "0B"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _human_eta(seconds: float | None) -> str:
    if not seconds:
        return "לא ידוע"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} שניות"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d} דקות"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d} שעות"


def format_progress_text(d: dict) -> str | None:
    status = d.get("status")
    if status == "downloading":
        downloaded = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        percent = int(downloaded / total * 100) if total else 0
        size_text = f"{_human_size(downloaded)}/{_human_size(total)}" if total else _human_size(downloaded)
        speed_text = f"{_human_size(d.get('speed'))}/s" if d.get("speed") else "לא ידוע"
        eta_text = _human_eta(d.get("eta"))
        return f"{texts.DOWNLOADING}\n{percent}% ({size_text})\n⚡ מהירות: {speed_text}\n⏱️ זמן משוער: {eta_text}"
    if status == "finished":
        return texts.PROCESSING
    return None


def _extract_file_paths(entry: dict) -> list[str]:
    downloads = entry.get("requested_downloads")
    if downloads:
        return [d["filepath"] for d in downloads if d.get("filepath")]
    filename = entry.get("filepath") or entry.get("_filename")
    return [filename] if filename else []


def _result_from_info(info: dict, dest_dir: Path) -> DownloadResult:
    file_paths: list[str] = []
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        for entry in entries:
            file_paths.extend(_extract_file_paths(entry))
        title = info.get("title") or info.get("description") or "Instagram"
    else:
        file_paths = _extract_file_paths(info)
        title = info.get("title") or info.get("description") or "Instagram"

    # Fallback to inspecting dest_dir if yt-dlp did not report filepaths explicitly
    if not file_paths and dest_dir.exists():
        found = [
            str(p)
            for p in sorted(dest_dir.rglob("*"))
            if p.is_file() and not p.name.endswith((".part", ".ytdl"))
        ]
        file_paths.extend(found)

    if not file_paths:
        raise InstagramDownloadError(classify_instagram_error(None))

    clean_title = title.strip().splitlines()[0] if title else "Instagram"
    return DownloadResult(
        file_paths=file_paths,
        title=clean_title[:100],
    )


class InstagramEngine(BaseEngine):
    """Instagram engine using yt-dlp with curl-cffi TLS impersonation."""

    name: str = "instagram"
    supported_platforms: tuple[str, ...] = ("instagram",)

    def __init__(
        self,
        *,
        max_download_size: int,
        progress=None,
        cookies_file: str | None = None,
        force_ipv4: bool = False,
        max_retries: int = 0,
    ) -> None:
        self._max_download_size = max_download_size
        self._progress = progress
        self._cookies_file = cookies_file or os.getenv("INSTAGRAM_COOKIES_FILE")
        self._force_ipv4 = force_ipv4
        self._max_retries = max_retries

    def matches(self, url: str) -> bool:
        return matches_instagram_url(url)

    async def download(
        self,
        url: str,
        *,
        dest_dir: Path,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        if not self.matches(url):
            raise UnsupportedUrlError(texts.UNSUPPORTED_URL)
        dest_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.to_thread(self._download_sync, url, dest_dir, loop, cancel_token)
        except DownloadTooLargeError:
            raise
        except UnsupportedUrlError:
            raise
        except InstagramDownloadError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error in Instagram download for %s", url)
            raise InstagramDownloadError(classify_instagram_error(str(exc))) from exc

    def _build_ydl_opts(
        self,
        dest_dir: Path,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
        guard: DownloadGuard | None = None,
    ) -> dict:
        guard = guard or DownloadGuard(self._max_download_size, cancel_token)
        opts: dict = {
            "outtmpl": str(dest_dir / "%(title).150s [%(id)s].%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [self._make_progress_hook(loop, cancel_token, guard)],
            "match_filter": guard.match_filter(),
            "retries": TRANSPORT_RETRIES,
            "fragment_retries": TRANSPORT_RETRIES,
            "noplaylist": True,
        }
        if self._force_ipv4:
            opts["source_address"] = "0.0.0.0"
        if self._cookies_file and os.path.exists(self._cookies_file):
            opts["cookiefile"] = self._cookies_file

        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget

            target_name = os.getenv("INSTAGRAM_IMPERSONATE", "chrome")
            opts["impersonate"] = ImpersonateTarget.from_str(target_name)
        except (ImportError, AttributeError, ValueError, TypeError) as exc:
            logger.debug("Could not configure explicit impersonation target: %s", exc)

        return opts

    def _make_progress_hook(
        self,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
        guard: DownloadGuard | None = None,
    ):
        state = {"last_forward": 0.0}
        guard = guard or DownloadGuard(self._max_download_size, cancel_token)

        def hook(d: dict) -> None:
            guard.check(d)
            if self._progress is None:
                return
            text = format_progress_text(d)
            if text is None:
                return
            now = time.monotonic()
            if d.get("status") != "downloading" or now - state["last_forward"] >= _PROGRESS_THROTTLE_SECONDS:
                state["last_forward"] = now
                try:
                    coro = self._progress.update(text, is_terminal=False)
                except TypeError:
                    coro = self._progress.update(text)
                asyncio.run_coroutine_threadsafe(coro, loop)

        return hook

    def _download_sync(
        self,
        url: str,
        dest_dir: Path,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        guard = DownloadGuard(self._max_download_size, cancel_token)
        ydl_opts = self._build_ydl_opts(dest_dir, loop, cancel_token, guard)
        last_message: str | None = None
        try:
            for attempt in range(self._max_retries + 1):
                if cancel_token is not None and cancel_token.is_set():
                    return DownloadResult(file_paths=[])
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                    remove_partial_files(dest_dir)
                    try:
                        return _result_from_info(info, dest_dir)
                    except InstagramDownloadError:
                        if guard.oversize:
                            raise too_large_error(guard.oversize[0]) from None
                        raise
                except DownloadCancelledSignal:
                    logger.info("Instagram download cancelled by timeout budget")
                    return DownloadResult(file_paths=[])
                except DownloadTooLargeSignal as exc:
                    raise too_large_error(exc) from exc
                except yt_dlp.utils.DownloadError as exc:
                    if isinstance(exc.__cause__, DownloadCancelledSignal):
                        logger.info("Instagram download cancelled by timeout budget")
                        return DownloadResult(file_paths=[])
                    if isinstance(exc.__cause__, DownloadTooLargeSignal):
                        raise too_large_error(exc.__cause__) from exc
                    if guard.oversize:
                        raise too_large_error(guard.oversize[0]) from exc
                    last_message = str(exc)
                    if attempt < self._max_retries and is_retryable_error(last_message):
                        self._log_retry(attempt, url, last_message)
                        time.sleep(min(2**attempt, 4))
                        continue
                    raise InstagramDownloadError(classify_instagram_error(last_message)) from exc
                except (InstagramDownloadError, DownloadTooLargeError):
                    raise
                except Exception as exc:
                    last_message = str(exc)
                    if attempt < self._max_retries and is_retryable_error(last_message):
                        self._log_retry(attempt, url, last_message)
                        time.sleep(min(2**attempt, 4))
                        continue
                    raise InstagramDownloadError(classify_instagram_error(last_message)) from exc
            raise InstagramDownloadError(classify_instagram_error(last_message))
        finally:
            remove_partial_files(dest_dir)

    def _log_retry(self, attempt: int, url: str, message: str) -> None:
        logger.warning(
            "Retrying Instagram download (attempt %s/%s) for %s: %s",
            attempt + 1,
            self._max_retries,
            url,
            message,
        )
