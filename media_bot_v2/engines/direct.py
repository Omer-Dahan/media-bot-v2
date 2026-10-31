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

import requests

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
from media_bot_v2.engines.hls import (
    HlsDownloadError,
    HlsUnsupportedError,
    detect_hls,
    download_hls,
    looks_like_hls_body,
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
    "facebook.com",
    "fb.com",
    "fb.watch",
    "fb.me",
)
_CHUNK_SIZE = 1024 * 1024
_REQUEST_TIMEOUT = 30
_PROGRESS_THROTTLE_SECONDS = 2.0

# 401/403/451: the site itself refuses to serve the file to this client.
# 404/410: the file is simply gone. Neither is fixed by retrying the exact
# same request, so these map to a hopeless domain error (no retry button)
# instead of leaking a raw requests.HTTPError up through the pipeline.
_REFUSED_STATUS_CODES = (401, 403, 451)
_GONE_STATUS_CODES = (404, 410)
# 429/5xx are transient (rate limiting, momentary server trouble) - still
# worth a retry, just with a classified message instead of the raw error.
_RETRYABLE_STATUS_CODES = (429,)


class DirectLinkRefusedError(UnsupportedUrlError):
    """Raised when the remote server returns 401/403/404/410/451 for a
    direct-link download - the site refuses to serve the file, or it no
    longer exists. Subclasses UnsupportedUrlError so pipeline.py's existing
    domain-error handling (single WARNING log, no traceback) picks it up."""


class DirectDownloadError(Exception):
    """Raised with an already-classified, Hebrew, user-facing message for a
    transient (429/5xx) direct-link failure - still worth a retry."""


# Maps engines.hls.HlsUnsupportedError.kind to the Hebrew text the user sees.
# M1 treats every active-key variant (SAMPLE-AES, identity AES-128, DRM
# keyformats) the same way from the user's point of view: this milestone
# only downloads clear streams, so all of them render as "protected content".
_HLS_KIND_TEXT: dict[str, str] = {
    "live": texts.HLS_LIVE_UNSUPPORTED,
    "malformed": texts.HLS_MALFORMED,
    "drm_unsupported": texts.HLS_DRM_UNSUPPORTED,
    "sample_aes_unsupported": texts.HLS_DRM_UNSUPPORTED,
    "encryption_unsupported": texts.HLS_DRM_UNSUPPORTED,
    "window_expired": texts.HLS_WINDOW_EXPIRED,
    "window_too_short": texts.HLS_WINDOW_TOO_SHORT,
}


def _download_hls(
    url: str,
    dest_dir: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None,
    segment_workers: int = 8,
    max_gap_ratio: float = 0.02,
) -> DownloadResult:
    """Runs the guarded HLS fetch/remux and translates its internal
    kind/debug-message classification into the Hebrew text the user sees,
    while keeping HlsUnsupportedError's UnsupportedUrlError ancestry so
    pipeline.py/retry.py's existing hopeless-classification keeps working."""
    try:
        return download_hls(
            url,
            dest_dir,
            max_size=max_size,
            cancel_token=cancel_token,
            segment_workers=segment_workers,
            max_gap_ratio=max_gap_ratio,
        )
    except HlsUnsupportedError as exc:
        logger.warning("HLS input unsupported (%s) for %s: %s", exc.kind, url, exc)
        raise _mark_logged(
            HlsUnsupportedError(exc.kind, _HLS_KIND_TEXT.get(exc.kind, texts.UNSUPPORTED_URL))
        ) from exc
    except HlsDownloadError as exc:
        logger.warning("HLS transient download failure for %s: %s", url, exc)
        raise _mark_logged(HlsDownloadError(texts.DOWNLOAD_FAILED)) from exc


def _classify_http_error(exc: requests.HTTPError, url: str) -> Exception:
    response = exc.response
    status = response.status_code if response is not None else None
    if status in _REFUSED_STATUS_CODES:
        logger.warning("Direct link refused (HTTP %s) for %s", status, url)
        return _mark_logged(DirectLinkRefusedError(texts.DIRECT_LINK_REFUSED))
    if status in _GONE_STATUS_CODES:
        logger.warning("Direct link gone (HTTP %s) for %s", status, url)
        return _mark_logged(DirectLinkRefusedError(texts.DIRECT_LINK_GONE))
    if status in _RETRYABLE_STATUS_CODES or (status is not None and 500 <= status < 600):
        logger.warning("Direct link server error (HTTP %s) for %s", status, url)
        return _mark_logged(DirectDownloadError(texts.DIRECT_LINK_SERVER_BUSY))
    return exc


def _mark_logged(exc: Exception) -> Exception:
    """Flags an exception as already reported at WARNING (with the HTTP
    status code and url) right above, so pipeline.py's generic domain-error
    handler - which only has the Hebrew user-facing text, not the status
    code - logs its own line at DEBUG instead of a second WARNING for the
    same failure."""
    exc.logged_by_engine = True
    return exc


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

    def __init__(
        self,
        *,
        max_download_size: int | None = None,
        progress=None,
        hls_segment_workers: int = 8,
        hls_max_gap_ratio: float = 0.02,
    ) -> None:
        self._max_download_size = max_download_size
        self._progress = progress
        self._hls_segment_workers = hls_segment_workers
        self._hls_max_gap_ratio = hls_max_gap_ratio

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
            result = await run_in_thread(
                _preflight_and_stream_to_file,
                url,
                dest_dir,
                self._max_download_size,
                cancel_token,
                self._progress,
                loop,
                self._hls_segment_workers,
                self._hls_max_gap_ratio,
            )
        except SSRFBlockedError as exc:
            logger.warning("Blocked SSRF attempt for %s: %s", url, exc)
            raise UnsupportedUrlError(texts.UNSUPPORTED_URL) from exc
        except requests.HTTPError as exc:
            classified = _classify_http_error(exc, url)
            if classified is exc:
                raise
            raise classified from exc
        if cancel_token is not None and cancel_token.is_set():
            return DownloadResult(file_paths=[])
        return result


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


def _filename_from_response(response) -> str:
    """The name to save this download under: the server's own suggested
    filename (`Content-Disposition`) if it offered one, else one derived
    from the final (post-redirect) URL's path - `response.url`, not the
    URL the caller originally requested, since a redirect can land on a
    server that names the file very differently from the original link.
    Either way, a missing extension is filled in from `Content-Type` as a
    stopgap - the authoritative fix for a missing or misleading extension is
    `media_probe.correct_extension`, which inspects the actual bytes once
    the file is fully on disk; this only keeps a server that sends no
    extension at all (and whose bytes `filetype` can't identify either,
    e.g. an ISO image or MSI installer) from producing a bare,
    extension-less filename."""
    cd_name = _parse_content_disposition_filename(response.headers.get("Content-Disposition"))
    if cd_name:
        name = safe_basename_from_url_path(cd_name, default_stem="download", default_ext="")
    else:
        name = _filename_from_url(response.url)
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
    hls_segment_workers: int = 8,
    hls_max_gap_ratio: float = 0.02,
) -> DownloadResult:
    head_content_type: str | None = None
    try:
        with safe_request("HEAD", url, timeout=10) as head_resp:
            if head_resp.status_code < 400:
                head_content_type = head_resp.headers.get("Content-Type")
                if not detect_hls(url, content_type=head_content_type):
                    reject_if_not_media_content_type(head_resp)
                    _reject_if_declared_size_too_large(head_resp, url, max_size)
    except (DownloadTooLargeError, UnsupportedUrlError, SSRFBlockedError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("Preflight HEAD request failed for %s: %s; proceeding to GET", url, exc)

    # Path extension or HEAD Content-Type already says HLS: go straight to
    # the HLS module without ever opening a direct-download destination file.
    if detect_hls(url, content_type=head_content_type):
        return _download_hls(
            url, dest_dir, max_size, cancel_token, hls_segment_workers, hls_max_gap_ratio
        )

    return _stream_to_file(
        url,
        dest_dir,
        max_size,
        cancel_token,
        progress,
        loop,
        hls_segment_workers,
        hls_max_gap_ratio,
    )


def _stream_to_file(
    url: str,
    dest_dir: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
    progress=None,
    loop=None,
    hls_segment_workers: int = 8,
    hls_max_gap_ratio: float = 0.02,
) -> DownloadResult:
    with safe_request("GET", url, stream=True, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        # GET's Content-Type can disagree with HEAD's, or HEAD may have
        # failed outright - re-check before any destination file is opened.
        if detect_hls(url, content_type=response.headers.get("Content-Type")):
            response.close()
            return _download_hls(
                url, dest_dir, max_size, cancel_token, hls_segment_workers, hls_max_gap_ratio
            )
        reject_if_not_media_content_type(response)
        _reject_if_declared_size_too_large(response, url, max_size)
        # Only decided now, not before the request: a redirect can land on a
        # server that names the file very differently from the original URL
        # (`Content-Disposition`), and `response` here is already the final
        # hop (`ssrf_guard.safe_request` re-validates and follows redirects
        # itself) - the HEAD preflight above is a best-effort optimization
        # only, never the source of the filename.
        dest_path = within_directory(
            dest_dir, _filename_from_response(response), fallback="download.bin"
        )
        if cancel_token is not None:
            if cancel_token.is_set():
                response.close()
                return DownloadResult(file_paths=[])
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
        sniffed = False
        f = None
        report_state = {"last_forward": 0.0}
        start_time = time.monotonic()
        try:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_token is not None and cancel_token.is_set():
                    response.close()
                    break
                if not chunk:
                    continue
                total += len(chunk)
                if not sniffed:
                    # Buffer without opening the destination file yet, so a
                    # buffered `#EXTM3U` prefix (no extension, no HLS
                    # Content-Type) can still redirect to download_hls
                    # before a single byte is ever written to disk.
                    head += chunk
                    if len(head) >= SNIFF_BYTES:
                        sniffed = True
                        if looks_like_hls_body(head):
                            response.close()
                            return _download_hls(
                                url,
                                dest_dir,
                                max_size,
                                cancel_token,
                                hls_segment_workers,
                                hls_max_gap_ratio,
                            )
                        reject_if_not_media_body(head)
                        if max_size is not None and total > max_size:
                            raise DownloadTooLargeError(
                                texts.format_download_too_large(total, max_size),
                                file_size=total,
                                max_size=max_size,
                                url=url,
                            )
                        f = open(dest_path, "wb")  # noqa: SIM115 - opened lazily after the sniff, closed in `finally` below
                        f.write(head)
                else:
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
                return DownloadResult(file_paths=[])
            if not sniffed:
                # The whole body was shorter than SNIFF_BYTES - sniff and
                # write it now, at end of stream.
                if looks_like_hls_body(head):
                    response.close()
                    return _download_hls(
                        url,
                        dest_dir,
                        max_size,
                        cancel_token,
                        hls_segment_workers,
                        hls_max_gap_ratio,
                    )
                if head:
                    reject_if_not_media_body(head)
                if max_size is not None and total > max_size:
                    raise DownloadTooLargeError(
                        texts.format_download_too_large(total, max_size),
                        file_size=total,
                        max_size=max_size,
                        url=url,
                    )
                f = open(dest_path, "wb")  # noqa: SIM115 - opened lazily after the sniff, closed in `finally` below
                f.write(head)
        except (DownloadTooLargeError, UnsupportedUrlError):
            dest_path.unlink(missing_ok=True)
            raise
        except Exception:
            dest_path.unlink(missing_ok=True)
            if cancel_token is not None and cancel_token.is_set():
                return DownloadResult(file_paths=[])
            raise
        finally:
            if f is not None:
                f.close()
    return DownloadResult(file_paths=[str(dest_path)], title=dest_path.name)


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
