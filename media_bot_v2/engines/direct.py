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
    reject_if_not_media_body,
    reject_if_not_media_content_type,
)
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
        filename = _filename_from_url(url)
        dest_path = dest_dir / filename
        loop = asyncio.get_running_loop() if self._progress is not None else None
        await asyncio.to_thread(
            _preflight_and_stream_to_file,
            url,
            dest_path,
            self._max_download_size,
            cancel_token,
            self._progress,
            loop,
        )
        if cancel_token is not None and cancel_token.is_set():
            return DownloadResult(file_paths=[])
        return DownloadResult(file_paths=[str(dest_path)], title=filename)


def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return unquote(name) or "download.bin"


def _preflight_and_stream_to_file(
    url: str,
    dest_path: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
    progress=None,
    loop=None,
) -> None:
    try:
        with requests.head(url, allow_redirects=True, timeout=10) as head_resp:
            if head_resp.status_code < 400:
                reject_if_not_media_content_type(head_resp)
                _reject_if_declared_size_too_large(head_resp, url, max_size)
    except (DownloadTooLargeError, UnsupportedUrlError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("Preflight HEAD request failed for %s: %s; proceeding to GET", url, exc)

    _stream_to_file(url, dest_path, max_size, cancel_token, progress, loop)


def _stream_to_file(
    url: str,
    dest_path: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
    progress=None,
    loop=None,
) -> None:
    with requests.get(url, stream=True, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        reject_if_not_media_content_type(response)
        _reject_if_declared_size_too_large(response, url, max_size)
        if cancel_token is not None:
            if cancel_token.is_set():
                response.close()
                return
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
                return
            if 0 < len(head) < SNIFF_BYTES:
                reject_if_not_media_body(head)
        except (DownloadTooLargeError, UnsupportedUrlError):
            dest_path.unlink(missing_ok=True)
            raise
        except Exception:
            dest_path.unlink(missing_ok=True)
            if cancel_token is not None and cancel_token.is_set():
                return
            raise


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
