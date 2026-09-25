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


class DirectEngine(BaseEngine):
    """Downloads an arbitrary HTTP(S) URL to disk via a streaming GET."""

    name: str = "direct"
    supported_platforms: tuple[str, ...] = ("direct",)

    def __init__(self, *, max_download_size: int | None = None) -> None:
        self._max_download_size = max_download_size

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
        await asyncio.to_thread(
            _preflight_and_stream_to_file,
            url,
            dest_path,
            self._max_download_size,
            cancel_token,
        )
        return DownloadResult(file_paths=[str(dest_path)], title=filename)


def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return unquote(name) or "download.bin"


def _preflight_and_stream_to_file(
    url: str,
    dest_path: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
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

    _stream_to_file(url, dest_path, max_size, cancel_token)


def _stream_to_file(
    url: str,
    dest_path: Path,
    max_size: int | None,
    cancel_token: CancellationToken | None = None,
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
        total = 0
        head = b""
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
