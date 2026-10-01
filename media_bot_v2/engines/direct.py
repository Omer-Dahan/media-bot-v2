"""Direct HTTP(S) link download engine.

This is the one engine implemented end-to-end in M1 (see spec/SPEC.md M1
item 7): download -> split -> upload -> charge credits -> delete from disk.
YouTube/TikTok/Instagram engines (src/engine/generic.py, tiktok.py,
instagram.py in the old bot) land in M2/M3 - this module only handles plain
HTTP(S) URLs that don't belong to a known platform.

Streaming download runs via `requests` (already a transitive dependency of
instaloader/gallery-dl) in a worker thread, since `requests` is synchronous
and this engine's `download()` must not block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.content_check import (
    SNIFF_BYTES,
    extension_for_content_type,
    reject_if_not_media_body,
    reject_if_not_media_content_type,
)
from media_bot_v2.engines.safe_filename import safe_basename_from_url_path, within_directory
from media_bot_v2.engines.ssrf_guard import SSRFBlockedError, safe_request
from media_bot_v2.executor import run_in_thread
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress_format import format_progress

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_KNOWN_PLATFORM_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "douyin.com",
    "instagram.com",
    "instagr.am",
)
_CHUNK_SIZE = 1024 * 1024
_REQUEST_TIMEOUT = 30
_PROGRESS_THROTTLE_SECONDS = 2.0


def _report_stream_progress(
    progress,
    loop,
    state: dict,
    start_time: float,
    *,
    transferred: int,
    total: int | None,
) -> None:
    """Mirrors providers.downloader._report_stream_progress (same
    fire-and-forget forwarding from a worker thread) so the direct-link
    engine's streaming GET shows the same bar/speed/ETA as every other route."""
    now = time.monotonic()
    if now - state["last_forward"] < _PROGRESS_THROTTLE_SECONDS:
        return
    state["last_forward"] = now
    elapsed = now - start_time
    speed = transferred / elapsed if elapsed > 0 else None
    eta = (total - transferred) / speed if (total is not None and speed) else None
    text = format_progress(
        f"⬇️ {texts.DOWNLOADING}",
        transferred=transferred,
        total=total,
        speed=speed,
        eta=eta,
    )
    try:
        coro = progress.update(text, is_terminal=False)
    except TypeError:
        coro = progress.update(text)
    asyncio.run_coroutine_threadsafe(coro, loop)


class DirectEngine(BaseEngine):
    """Downloads an arbitrary HTTP(S) URL to disk via a streaming GET."""

    name: str = "direct"
    supported_platforms: tuple[str, ...] = ("direct",)

    def __init__(self, *, max_download_size: int | None = None, progress=None) -> None:
        self._max_download_size = max_download_size
        self._progress = progress

    def matches(self, url: str) -> bool:
        if not _URL_RE.match(url):
            return False
        try:
            host = urlparse(url).netloc.split(":")[0].lower()
        except (ValueError, AttributeError):
            return False
        if not host:
            return False
        return not any(host == d or host.endswith("." + d) for d in _KNOWN_PLATFORM_DOMAINS)

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
        loop = asyncio.get_running_loop() if self._progress is not None else None
        try:
            dest_path = await run_in_thread(
                _preflight_and_stream_to_file,
                url,
                dest_dir,
                self._max_download_size,
                cancel_token,
                self._progress,
                loop,
            )
        except SSRFBlockedError as exc:
            logger.warning("Blocked SSRF attempt for %s: %s", url, exc)
            raise UnsupportedUrlError(texts.UNSUPPORTED_URL) from exc
        if cancel_token is not None and cancel_token.is_set():
            return DownloadResult(file_paths=[])
        return DownloadResult(file_paths=[str(dest_path)], title=dest_path.name)


_CD_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*[^']*''([^;]+)", re.IGNORECASE)
_CD_FILENAME_RE = re.compile(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)', re.IGNORECASE)


def _parse_content_disposition_filename(header_value: str | None) -> str | None:
    """Extract a filename from a `Content-Disposition` header, preferring
    the RFC 5987 `filename*=UTF-8''...` form (percent-encoded, so it survives
    non-ASCII names) over the plain `filename="..."` form. None if the
    header is absent or has neither form."""
    if not header_value:
        return None
    star_match = _CD_FILENAME_STAR_RE.search(header_value)
    if star_match:
        candidate = unquote(star_match.group(1).strip())
        if candidate:
            return candidate
    match = _CD_FILENAME_RE.search(header_value)
    if match:
        candidate = (match.group(1) or match.group(2) or "").strip().strip('"')
        if candidate:
            return candidate
    return None


def _filename_from_url(url: str) -> str:
    return safe_basename_from_url_path(urlparse(url).path, default_stem="download", default_ext="")


def _filename_from_response(url: str, response) -> str:
    """The name to save this download under: the server's own suggested
    filename (`Content-Disposition`) if it offered one, else one derived
    from the URL's path. Either way, a missing extension is filled in from
    `Content-Type` as a stopgap - the authoritative fix for a missing or
    misleading extension is `media_probe.correct_extension`, which inspects
    the actual bytes once the file is fully on disk; this only keeps a
    server that sends no extension at all (and whose bytes `filetype` can't
    identify either, e.g. an ISO image or MSI installer) from producing a
    bare, extension-less filename."""
    cd_name = _parse_content_disposition_filename(response.headers.get("Content-Disposition"))
    if cd_name:
        name = safe_basename_from_url_path(cd_name, default_stem="download", default_ext="")
    else:
        name = _filename_from_url(url)
    if not name:
        name = "download"
    if not Path(name).suffix:
        ext = extension_for_content_type(response.headers.get("Content-Type")) or ".bin"
        name = f"{name}{ext}"
    return name


def _preflight_and_stream_to_file(
    url: str,
    dest_dir: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
    progress=None,
    loop=None,
) -> Path:
    try:
        with safe_request("HEAD", url, timeout=10) as head_resp:
            if head_resp.status_code < 400:
                reject_if_not_media_content_type(head_resp)
                _reject_if_declared_size_too_large(head_resp, url, max_size)
    except (DownloadTooLargeError, UnsupportedUrlError, SSRFBlockedError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("Preflight HEAD request failed for %s: %s; proceeding to GET", url, exc)

    return _stream_to_file(url, dest_dir, max_size, cancel_token, progress, loop)


def _stream_to_file(
    url: str,
    dest_dir: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
    progress=None,
    loop=None,
) -> Path:
    with safe_request("GET", url, stream=True, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        reject_if_not_media_content_type(response)
        _reject_if_declared_size_too_large(response, url, max_size)
        # Only decided now, not before the request: a redirect can land on a
        # server that names the file very differently from the original URL
        # (`Content-Disposition`), and `response` here is already the final
        # hop (`ssrf_guard.safe_request` re-validates and follows redirects
        # itself) - the HEAD preflight above is a best-effort optimization
        # only, never the source of the filename.
        dest_path = within_directory(
            dest_dir, _filename_from_response(url, response), fallback="download.bin"
        )
        if cancel_token is not None:
            if cancel_token.is_set():
                response.close()
                return dest_path
            cancel_token.on_cancel(response.close)

        # Parsed once: also the total shown in the progress bar below
        # (omitted rather than shown as a fake total with no Content-Length).
        declared_total: int | None = None
        declared_header = response.headers.get("Content-Length")
        if declared_header is not None:
            try:
                declared_total = int(declared_header)
            except ValueError:
                declared_total = None

        total = 0
        head = b""
        report_state = {"last_forward": 0.0}
        start_time = time.monotonic()
        try:
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if cancel_token is not None and cancel_token.is_set():
                        response.close()
                        break
                    if not chunk:
                        continue
                    # Sniff once enough bytes have arrived (a first chunk can be
                    # tiny); the tail of a short body is checked after the loop.
                    if len(head) < SNIFF_BYTES:
                        head += chunk[: SNIFF_BYTES - len(head)]
                        if len(head) >= SNIFF_BYTES:
                            reject_if_not_media_body(head)
                    total += len(chunk)
                    if max_size is not None and total > max_size:
                        raise DownloadTooLargeError(
                            texts.format_download_too_large(total, max_size),
                            file_size=total,
                            max_size=max_size,
                            url=url,
                        )
                    f.write(chunk)
                    if progress is not None and loop is not None:
                        _report_stream_progress(
                            progress,
                            loop,
                            report_state,
                            start_time,
                            transferred=total,
                            total=declared_total,
                        )
            if cancel_token is not None and cancel_token.is_set():
                # Cancelled mid-stream: what is on disk is a truncated file, not a result.
                dest_path.unlink(missing_ok=True)
                return dest_path
            if 0 < len(head) < SNIFF_BYTES:
                reject_if_not_media_body(head)
        except (DownloadTooLargeError, UnsupportedUrlError):
            dest_path.unlink(missing_ok=True)
            raise
        except Exception:
            dest_path.unlink(missing_ok=True)
            if cancel_token is not None and cancel_token.is_set():
                return dest_path
            raise
    return dest_path


def _reject_if_declared_size_too_large(response, url: str, max_size: int | None) -> None:
    """Fail before writing a single byte if the server already declared a
    too-large size, instead of discovering it after streaming gigabytes to
    disk (the chunk-by-chunk check below still catches a server that lies
    about or omits Content-Length)."""
    if max_size is None:
        return
    declared = response.headers.get("Content-Length")
    if declared is None:
        return
    try:
        declared_size = int(declared)
    except ValueError:
        return
    if declared_size > max_size:
        raise DownloadTooLargeError(
            texts.format_download_too_large(declared_size, max_size),
            file_size=declared_size,
            max_size=max_size,
            url=url,
        )
