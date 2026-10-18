"""Facebook download engine backed by yt-dlp with curl-cffi impersonation.

Modeled on engines/instagram.py:
- Uses yt-dlp with browser impersonation (curl-cffi) for public content
  extraction, defaulting to anonymous extraction without requiring cookies.
- Strict error classification with Hebrew messages for private/login-required,
  not found/deleted, unsupported media/live streams, and network errors.
- Progress reporting and mid-download size cap enforcement via progress hooks.
- One attempt per route (`max_retries=0`); transport retries happen inside
  yt-dlp only (see engines/ytdlp_support.py).

Facebook-specific wrinkles yt-dlp's FacebookIE/FacebookReelIE do not handle on
their own:
- `/share/...` links and `fb.watch`/`fb.me` short links are resolved by
  following redirects manually (`resolve_facebook_url` below), never handed
  to yt-dlp as-is - every hop is re-validated against the Facebook domain
  list (SSRF guard: a redirect that leaves Facebook is never followed).
- Facebook's progressive `hd`/`sd` formats are H.264+AAC; its DASH formats
  are AV1+AAC, and AV1-in-MP4 is not Telegram-client-streamable (lesson
  M9.1). The single format selector below always prefers the progressive
  H.264 formats and only falls back to DASH/AV1 as a last resort.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
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
    YtdlpLoggerAdapter,
    remove_partial_files,
    rename_to_safe_display_name,
    safe_outtmpl,
    too_large_error,
)
from media_bot_v2.executor import run_in_thread
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress_format import format_progress

logger = logging.getLogger(__name__)

_PROGRESS_THROTTLE_SECONDS = 2.0

_FACEBOOK_DOMAINS = ("facebook.com", "fb.com")
_FACEBOOK_SHORT_DOMAINS = ("fb.watch", "fb.me")
_ALL_FACEBOOK_DOMAINS = _FACEBOOK_DOMAINS + _FACEBOOK_SHORT_DOMAINS

_MAX_REDIRECT_HOPS = 5
_RESOLVE_TIMEOUT_SECONDS = 10

# Single format expression, in fallthrough order: progressive H.264 MP4
# first (Telegram-client-streamable), then DASH H.264, then any MP4 with an
# AVC video track, then a manual bestvideo(AVC)+bestaudio pairing, then
# whatever MP4 is available, then anything at all (AV1/VP9 DASH included).
FORMAT_SELECTOR = (
    "hd[ext=mp4]/sd[ext=mp4]/best[ext=mp4][vcodec^=avc]/"
    "bestvideo[ext=mp4][vcodec^=avc]+bestaudio[acodec^=mp4a]/best[ext=mp4]/best"
)

_REEL_PATH_RE = re.compile(r"^/reels?/(\d+)", re.IGNORECASE)
_PAGE_VIDEO_PATH_RE = re.compile(r"^/[^/]+/videos/([^/?#]+)", re.IGNORECASE)
_BARE_VIDEO_PATH_RE = re.compile(r"^/videos/([^/?#]+)", re.IGNORECASE)
_FACEBOOK_VIDEO_PATH_RE = re.compile(
    r"^/(?:"
    r"reels?/\d+/?$"
    r"|video\.php"
    r"|story\.php"
    r"|permalink\.php"
    r"|plugins/video\.php"
    r"|groups/[^/]+/posts/[^/?#]+"
    r"|share/(?:r|v)/[^/?#]+"
    r"|share/[^/?#]+/?$"
    r"|videos/[^/?#]+"
    r"|[^/]+/videos/[^/?#]+"
    r")",
    re.IGNORECASE,
)

# Facebook's own counter prefix on a video's `description`, e.g.
# "1.6K views · 13K reactions ｜ actual title" - stripped before display since
# it is not part of the title a user would recognize as such.
_COUNTER_SEGMENT = r"[\d.,]+[KMB]?\s+(?:views|reactions|comments|shares)"
_TITLE_PREFIX_RE = re.compile(
    rf"^(?:{_COUNTER_SEGMENT})(?:\s*[·•]\s*(?:{_COUNTER_SEGMENT}))*\s*[｜|]\s*",
    re.IGNORECASE,
)


class FacebookDownloadError(Exception):
    """Raised with an already-classified, Hebrew, user-facing message."""


_DownloadTooLargeSignal = DownloadTooLargeSignal
_DownloadCancelledSignal = DownloadCancelledSignal


def _host_is_facebook(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _ALL_FACEBOOK_DOMAINS)


def _host_is_short(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _FACEBOOK_SHORT_DOMAINS)


def matches_facebook_url(url: str) -> bool:
    """Check if the URL belongs to Facebook and has a supported video path.

    Supports facebook.com, fb.com, fb.watch, fb.me and subdomains (www, m,
    web, mobile). Photo/album/profile/marketplace pages return False.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower()
    if not _host_is_facebook(host):
        return False
    path = parsed.path or "/"
    if _host_is_short(host):
        return bool(path.strip("/"))
    if path.rstrip("/") == "/watch":
        return bool(parse_qs(parsed.query).get("v"))
    return bool(_FACEBOOK_VIDEO_PATH_RE.match(path))


def extract_facebook_id(url: str) -> str | None:
    """Extract a stable identifier from a Facebook URL for cache keying:
    the reel/video numeric id, the `v=` query param, or - for a share/short
    link whose token is the only identifier available - a hash of that
    token (never the raw token itself, which is not guaranteed numeric or
    filesystem/cache-key safe)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower()
    if not _host_is_facebook(host):
        return None
    path = parsed.path or ""

    match = _REEL_PATH_RE.match(path)
    if match:
        return match.group(1)

    query_v = parse_qs(parsed.query).get("v")
    if query_v and query_v[0]:
        return query_v[0]

    match = _PAGE_VIDEO_PATH_RE.match(path) or _BARE_VIDEO_PATH_RE.match(path)
    if match:
        token = match.group(1)
        return token if token.isdigit() else hashlib.sha1(token.encode()).hexdigest()[:16]

    if _host_is_short(host) or path.startswith("/share/"):
        token = path.rstrip("/").rsplit("/", 1)[-1]
        if token:
            return token if token.isdigit() else hashlib.sha1(token.encode()).hexdigest()[:16]

    return None


def _is_tracking_param(key: str) -> bool:
    # Facebook's own `__cft__`/`__tn__` trackers are sent as array-style keys
    # (`__cft__[0]`, `__tn__[1]`), hence the prefix check rather than an exact
    # match for those two.
    return key in ("mibextid", "_rdr") or key.startswith(("__cft__", "__tn__"))


def _strip_tracking_params(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [
        (key, value)
        for key, value in (p.split("=", 1) if "=" in p else (p, "") for p in parsed.query.split("&") if p)
        if not _is_tracking_param(key)
    ]
    new_query = "&".join(f"{k}={v}" if v else k for k, v in kept)
    return urlunparse(parsed._replace(query=new_query))


def resolve_facebook_url(url: str) -> str:
    """Follow redirects for `/share/...` and `fb.watch`/`fb.me` short links,
    stopping at the first hop whose target leaves the Facebook domains (SSRF
    guard - that hop is never fetched) and returning the last confirmed
    Facebook URL reached. Never raises; a resolution failure or a URL that
    did not need resolving both just return the input unchanged, so a caller
    can always fall through to handing the original URL to yt-dlp."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.netloc or "").lower()
    if not (_host_is_short(host) or (parsed.path or "").startswith("/share/")):
        return url

    current = _strip_tracking_params(url)
    for _ in range(_MAX_REDIRECT_HOPS):
        try:
            response = requests.get(
                current,
                allow_redirects=False,
                timeout=_RESOLVE_TIMEOUT_SECONDS,
                headers={"User-Agent": "Mozilla/5.0"},
            )
        except requests.RequestException as exc:
            logger.debug("Facebook short-link resolution request failed for %s: %s", current, exc)
            return current
        if response.status_code not in (301, 302, 303, 307, 308):
            return current
        location = response.headers.get("Location")
        if not location:
            return current
        next_url = urljoin(current, location)
        next_host = (urlparse(next_url).netloc or "").lower()
        if not _host_is_facebook(next_host):
            logger.warning(
                "Facebook short-link redirect left Facebook domains (host=%s); stopping resolution",
                next_host,
            )
            return current
        current = _strip_tracking_params(next_url)
    return current


_ERROR_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "login required",
            "you must log in",
            "requires a login",
            "log in to continue",
            "age-restricted",
            "age restricted",
            "cookies",
            "checkpoint",
            "sign in",
            "confirm you're not a bot",
            "bot detection",
            "http error 401",
            "http error 403",
        ),
        texts.FACEBOOK_PRIVATE_OR_LOGIN,
    ),
    (
        (
            "this video is no longer available",
            "content isn't available",
            "content is not available",
            "video unavailable",
            "post is unavailable",
            "not found",
            "does not exist",
            "has been removed",
            "http error 404",
        ),
        texts.FACEBOOK_NOT_FOUND,
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
            "is live",
        ),
        texts.FACEBOOK_UNSUPPORTED_MEDIA,
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


def classify_facebook_error(message: str | None) -> str:
    """Translate yt-dlp / network error message to a precise Hebrew diagnostic.

    Whitelist only, like `classify_instagram_error`: the user sees a fixed
    message and never any part of `message` (paths, hosts, IPs, tokens); the
    raw text is logged instead.
    """
    if not message:
        return "❌ ההורדה נכשלה: לא התקבל קובץ מדיה מפייסבוק.\n➡️ נסה שוב או שלח קישור אחר."
    lowered = message.lower()
    for keywords, hebrew in _ERROR_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            return hebrew
    if any(pattern in lowered for pattern in _NETWORK_PATTERNS):
        return texts.FACEBOOK_NETWORK_ERROR
    logger.warning("Unclassified Facebook error (details withheld from user): %s", message)
    return texts.FACEBOOK_GENERIC_FAILURE


def is_retryable_error(message: str | None) -> bool:
    """Only network dropouts and 5xx server issues qualify for retry."""
    if not message:
        return False
    lowered = message.lower()
    return any(pattern in lowered for pattern in _NETWORK_PATTERNS) or "http error 5" in lowered


def format_progress_text(d: dict) -> str | None:
    status = d.get("status")
    if status == "downloading":
        return format_progress(
            f"⬇️ {texts.DOWNLOADING}",
            transferred=d.get("downloaded_bytes"),
            total=d.get("total_bytes") or d.get("total_bytes_estimate"),
            speed=d.get("speed"),
            eta=d.get("eta"),
        )
    if status == "finished":
        return texts.PROCESSING
    return None


def _clean_title(raw: str | None) -> str:
    if not raw:
        return "Facebook"
    first_line = raw.strip().splitlines()[0] if raw.strip() else ""
    if not first_line:
        return "Facebook"
    cleaned = _TITLE_PREFIX_RE.sub("", first_line, count=1).strip()
    cleaned = cleaned or first_line
    return cleaned[:100] or "Facebook"


def _is_live(entry: dict) -> bool:
    return bool(entry.get("is_live"))


def _extract_file_paths(entry: dict) -> list[str]:
    downloads = entry.get("requested_downloads")
    if downloads:
        paths = [d["filepath"] for d in downloads if d.get("filepath")]
    else:
        filename = entry.get("filepath") or entry.get("_filename")
        paths = [filename] if filename else []
    return [rename_to_safe_display_name(entry, p) for p in paths]


def _result_from_info(info: dict, dest_dir: Path) -> DownloadResult:
    file_paths: list[str] = []
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if any(_is_live(e) for e in entries):
            raise FacebookDownloadError(classify_facebook_error("live stream"))
        for entry in entries:
            file_paths.extend(_extract_file_paths(entry))
        raw_title = info.get("description") or info.get("title")
    else:
        if _is_live(info):
            raise FacebookDownloadError(classify_facebook_error("live stream"))
        file_paths = _extract_file_paths(info)
        raw_title = info.get("description") or info.get("title")

    # Fallback to inspecting dest_dir if yt-dlp did not report filepaths explicitly
    if not file_paths and dest_dir.exists():
        found = [
            str(p)
            for p in sorted(dest_dir.rglob("*"))
            if p.is_file() and not p.name.endswith((".part", ".ytdl"))
        ]
        file_paths.extend(rename_to_safe_display_name(info, p) for p in found)

    if not file_paths:
        raise FacebookDownloadError(classify_facebook_error(None))

    return DownloadResult(file_paths=file_paths, title=_clean_title(raw_title))


class FacebookEngine(BaseEngine):
    """Facebook engine using yt-dlp with curl-cffi TLS impersonation."""

    name: str = "facebook"
    supported_platforms: tuple[str, ...] = ("facebook",)

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
        self._cookies_file = cookies_file or os.getenv("FACEBOOK_COOKIES_FILE")
        self._force_ipv4 = force_ipv4
        self._max_retries = max_retries

    def matches(self, url: str) -> bool:
        return matches_facebook_url(url)

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
            return await run_in_thread(self._download_sync, url, dest_dir, loop, cancel_token)
        except DownloadTooLargeError:
            raise
        except UnsupportedUrlError:
            raise
        except FacebookDownloadError:
            raise
        except Exception as exc:
            logger.exception("Unexpected error in Facebook download for %s", url)
            raise FacebookDownloadError(classify_facebook_error(str(exc))) from exc

    def _build_ydl_opts(
        self,
        dest_dir: Path,
        loop: asyncio.AbstractEventLoop,
        cancel_token: CancellationToken | None = None,
        guard: DownloadGuard | None = None,
    ) -> dict:
        guard = guard or DownloadGuard(self._max_download_size, cancel_token)
        opts: dict = {
            "outtmpl": safe_outtmpl(dest_dir),
            "format": FORMAT_SELECTOR,
            "quiet": True,
            "no_warnings": False,
            "logger": YtdlpLoggerAdapter(),
            "noprogress": True,
            "progress_hooks": [self._make_progress_hook(loop, cancel_token, guard)],
            "match_filter": guard.match_filter(),
            "retries": TRANSPORT_RETRIES,
            "fragment_retries": TRANSPORT_RETRIES,
            "merge_output_format": "mp4",
        }
        if self._force_ipv4:
            opts["source_address"] = "0.0.0.0"
        if self._cookies_file and os.path.exists(self._cookies_file):
            opts["cookiefile"] = self._cookies_file

        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget

            target_name = os.getenv("FACEBOOK_IMPERSONATE", "chrome")
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
        target_url = resolve_facebook_url(url)
        guard = DownloadGuard(self._max_download_size, cancel_token)
        ydl_opts = self._build_ydl_opts(dest_dir, loop, cancel_token, guard)
        last_message: str | None = None
        try:
            for attempt in range(self._max_retries + 1):
                if cancel_token is not None and cancel_token.is_set():
                    return DownloadResult(file_paths=[])
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(target_url, download=True)
                    remove_partial_files(dest_dir)
                    try:
                        return _result_from_info(info, dest_dir)
                    except FacebookDownloadError:
                        if guard.oversize:
                            raise too_large_error(guard.oversize[0]) from None
                        raise
                except DownloadCancelledSignal:
                    logger.info("Facebook download cancelled by timeout budget")
                    return DownloadResult(file_paths=[])
                except DownloadTooLargeSignal as exc:
                    raise too_large_error(exc) from exc
                except yt_dlp.utils.DownloadError as exc:
                    if isinstance(exc.__cause__, DownloadCancelledSignal):
                        logger.info("Facebook download cancelled by timeout budget")
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
                    raise FacebookDownloadError(classify_facebook_error(last_message)) from exc
                except (FacebookDownloadError, DownloadTooLargeError):
                    raise
                except Exception as exc:
                    last_message = str(exc)
                    if attempt < self._max_retries and is_retryable_error(last_message):
                        self._log_retry(attempt, url, last_message)
                        time.sleep(min(2**attempt, 4))
                        continue
                    raise FacebookDownloadError(classify_facebook_error(last_message)) from exc
            raise FacebookDownloadError(classify_facebook_error(last_message))
        finally:
            remove_partial_files(dest_dir)

    def _log_retry(self, attempt: int, url: str, message: str) -> None:
        logger.warning(
            "Retrying Facebook download (attempt %s/%s) for %s: %s",
            attempt + 1,
            self._max_retries,
            url,
            message,
        )
