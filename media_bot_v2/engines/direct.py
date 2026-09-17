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
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from media_bot_v2.engines.base import BaseEngine, DownloadResult, DownloadTooLargeError

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_CHUNK_SIZE = 1024 * 1024
_REQUEST_TIMEOUT = 30


class DirectEngine(BaseEngine):
    """Downloads an arbitrary HTTP(S) URL to disk via a streaming GET."""

    def __init__(self, *, max_download_size: int | None = None) -> None:
        self._max_download_size = max_download_size

    def matches(self, url: str) -> bool:
        return bool(_URL_RE.match(url))

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        filename = _filename_from_url(url)
        dest_path = dest_dir / filename
        await asyncio.to_thread(_stream_to_file, url, dest_path, self._max_download_size)
        return DownloadResult(file_paths=[str(dest_path)], title=filename)


def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return unquote(name) or "download.bin"


def _stream_to_file(url: str, dest_path: Path, max_size: int | None) -> None:
    with requests.get(url, stream=True, timeout=_REQUEST_TIMEOUT) as response:
        response.raise_for_status()
        _reject_if_declared_size_too_large(response, url, max_size)
        total = 0
        try:
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_size is not None and total > max_size:
                        raise DownloadTooLargeError(
                            f"Download exceeded the {max_size} byte limit for {url}"
                        )
                    f.write(chunk)
        except DownloadTooLargeError:
            dest_path.unlink(missing_ok=True)
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
            f"Declared Content-Length {declared_size} exceeds the {max_size} byte limit for {url}"
        )
