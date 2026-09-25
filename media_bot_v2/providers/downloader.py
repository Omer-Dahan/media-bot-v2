"""Streaming downloader for direct media URLs returned by providers."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from media_bot_v2.engines.base import (
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    NotMediaContentError,
)
from media_bot_v2.engines.content_check import (
    SNIFF_BYTES,
    reject_if_not_media_body,
    reject_if_not_media_content_type,
)
from media_bot_v2.providers.base import ProviderResult
from media_bot_v2.telegram import texts

_CHUNK_SIZE = 1024 * 1024
_DEFAULT_TIMEOUT = 30
_SAFE_FILENAME_RE = re.compile(r'[\\/*?:"<>|]')


def _sanitize_title(title: str | None) -> str:
    if not title:
        return "download"
    sanitized = _SAFE_FILENAME_RE.sub("_", title).strip()
    return sanitized[:100] or "download"


def _filename_for_item(result: ProviderResult, url: str, index: int, total: int) -> str:
    if total > 1 and result.media_type == "photo":
        ext = ".jpg"
        parsed = urlparse(url)
        path_ext = Path(parsed.path).suffix.lower()
        if path_ext in (".jpg", ".jpeg", ".png", ".webp"):
            ext = path_ext
        return f"photo_{index:03d}{ext}"

    # Single item or non-photo
    parsed = urlparse(url)
    url_name = unquote(Path(parsed.path).name)
    url_ext = Path(url_name).suffix.lower()

    if not url_ext:
        if result.media_type == "video":
            url_ext = ".mp4"
        elif result.media_type == "audio":
            url_ext = ".mp3"
        elif result.media_type == "photo":
            url_ext = ".jpg"
        else:
            url_ext = ".bin"

    if result.title:
        base_name = _sanitize_title(result.title)
        return f"{base_name}{url_ext}"

    return url_name or f"download{url_ext}"


def _stream_url_to_file(
    url: str,
    dest_path: Path,
    max_size: int | None,
    headers: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    downloaded_so_far: int = 0,
    cancel_token: CancellationToken | None = None,
) -> int:
    """Stream url to dest_path, return the number of bytes written.

    max_size is the cap for the whole task, not just this file: callers
    downloading multiple items (e.g. a TikWM photo slideshow) pass in
    downloaded_so_far so a slideshow of many individually-small images still
    gets rejected once their sum crosses the limit.
    """
    req_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
    }
    if headers:
        req_headers.update(headers)

    with requests.get(url, stream=True, timeout=timeout, headers=req_headers) as response:
        response.raise_for_status()
        reject_if_not_media_content_type(response)
        if cancel_token is not None:
            if cancel_token.is_set():
                response.close()
                return 0
            cancel_token.on_cancel(response.close)

        # Reject early if server declared Content-Length exceeds the remaining budget
        if max_size is not None:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    decl_size = int(declared)
                    if downloaded_so_far + decl_size > max_size:
                        raise DownloadTooLargeError(
                            texts.format_download_too_large(downloaded_so_far + decl_size, max_size),
                            file_size=downloaded_so_far + decl_size,
                            max_size=max_size,
                            url=url,
                        )
                except ValueError:
                    pass

        total_bytes = 0
        head = b""
        try:
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if cancel_token is not None and cancel_token.is_set():
                        response.close()
                        break
                    if not chunk:
                        continue
                    if len(head) < SNIFF_BYTES:
                        head += chunk[: SNIFF_BYTES - len(head)]
                        if len(head) >= SNIFF_BYTES:
                            reject_if_not_media_body(head)
                    total_bytes += len(chunk)
                    if max_size is not None and downloaded_so_far + total_bytes > max_size:
                        raise DownloadTooLargeError(
                            texts.format_download_too_large(downloaded_so_far + total_bytes, max_size),
                            file_size=downloaded_so_far + total_bytes,
                            max_size=max_size,
                            url=url,
                        )
                    f.write(chunk)
            if cancel_token is not None and cancel_token.is_set():
                # Cancelled mid-stream: a truncated file must not survive as a result.
                dest_path.unlink(missing_ok=True)
                return 0
            if 0 < len(head) < SNIFF_BYTES:
                reject_if_not_media_body(head)
        except (DownloadTooLargeError, NotMediaContentError):
            dest_path.unlink(missing_ok=True)
            raise
        except Exception:
            dest_path.unlink(missing_ok=True)
            if cancel_token is not None and cancel_token.is_set():
                return 0
            raise
    return total_bytes


async def download_provider_media(
    result: ProviderResult,
    dest_dir: Path,
    *,
    max_size: int | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    cancel_token: CancellationToken | None = None,
) -> DownloadResult:
    """Stream all media URLs in ProviderResult to dest_dir, returning DownloadResult."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths: list[str] = []

    total_items = len(result.media_urls)
    total_downloaded = 0
    for idx, media_url in enumerate(result.media_urls, start=1):
        if cancel_token is not None and cancel_token.is_set():
            break
        filename = _filename_for_item(result, media_url, idx, total_items)
        dest_path = dest_dir / filename
        bytes_written = await asyncio.to_thread(
            _stream_url_to_file,
            media_url,
            dest_path,
            max_size,
            result.headers,
            timeout,
            downloaded_so_far=total_downloaded,
            cancel_token=cancel_token,
        )
        if cancel_token is not None and cancel_token.is_set():
            break
        total_downloaded += bytes_written
        downloaded_paths.append(str(dest_path))

    return DownloadResult(
        file_paths=downloaded_paths,
        title=result.title,
        description=result.description,
    )
