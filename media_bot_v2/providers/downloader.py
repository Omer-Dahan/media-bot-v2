"""Streaming downloader for direct media URLs returned by providers."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse

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
from media_bot_v2.engines.safe_filename import (
    build_safe_named_file,
    safe_basename_from_url_path,
    within_directory,
)
from media_bot_v2.engines.ssrf_guard import safe_request
from media_bot_v2.providers.base import ProviderResult
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress_format import format_progress

_CHUNK_SIZE = 1024 * 1024
_DEFAULT_TIMEOUT = 30
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
    """Forward a throttled progress update from the worker thread doing the
    streaming GET to the event loop, mirroring
    engines.youtube.YouTubeEngine._make_progress_hook so a provider fallback
    download (no yt-dlp progress_hooks here) looks like the same bot. Purely
    fire-and-forget - a slow or failing progress edit must never block or
    fail the download itself."""
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


_DEFAULT_EXT_BY_MEDIA_TYPE = {"video": ".mp4", "audio": ".mp3", "photo": ".jpg"}


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
    default_ext = _DEFAULT_EXT_BY_MEDIA_TYPE.get(result.media_type, ".bin")

    if result.title:
        # The URL, if it has a recognizable extension, still decides the
        # extension; the title (untrusted, provider- or user-supplied) only
        # supplies the stem, and is never used to build a path (finding A).
        url_name = safe_basename_from_url_path(parsed.path, default_ext="")
        url_ext = Path(url_name).suffix.lower() or default_ext
        return build_safe_named_file(result.title, suffix=url_ext)

    return safe_basename_from_url_path(parsed.path, default_stem="download", default_ext=default_ext)


def _stream_url_to_file(
    url: str,
    dest_path: Path,
    max_size: int | None,
    headers: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    downloaded_so_far: int = 0,
    cancel_token: CancellationToken | None = None,
    progress=None,
    loop=None,
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

    with safe_request("GET", url, stream=True, timeout=timeout, headers=req_headers) as response:
        response.raise_for_status()
        reject_if_not_media_content_type(response)
        if cancel_token is not None:
            if cancel_token.is_set():
                response.close()
                return 0
            cancel_token.on_cancel(response.close)

        # Parsed once regardless of max_size: also the total shown in the
        # progress bar below (omitted rather than shown as a fake total when
        # the server sent no Content-Length).
        declared_total: int | None = None
        declared_header = response.headers.get("Content-Length")
        if declared_header is not None:
            try:
                declared_total = int(declared_header)
            except ValueError:
                declared_total = None

        # Reject early if server declared Content-Length exceeds the remaining budget
        if (
            max_size is not None
            and declared_total is not None
            and downloaded_so_far + declared_total > max_size
        ):
            raise DownloadTooLargeError(
                texts.format_download_too_large(downloaded_so_far + declared_total, max_size),
                file_size=downloaded_so_far + declared_total,
                max_size=max_size,
                url=url,
            )

        total_bytes = 0
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
                    if progress is not None and loop is not None:
                        _report_stream_progress(
                            progress,
                            loop,
                            report_state,
                            start_time,
                            transferred=total_bytes,
                            total=declared_total,
                        )
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
    progress=None,
) -> DownloadResult:
    """Stream all media URLs in ProviderResult to dest_dir, returning DownloadResult."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths: list[str] = []

    loop = asyncio.get_running_loop() if progress is not None else None
    total_items = len(result.media_urls)
    total_downloaded = 0
    for idx, media_url in enumerate(result.media_urls, start=1):
        if cancel_token is not None and cancel_token.is_set():
            break
        filename = _filename_for_item(result, media_url, idx, total_items)
        dest_path = within_directory(dest_dir, filename, fallback=f"download_{idx:03d}.bin")
        bytes_written = await asyncio.to_thread(
            _stream_url_to_file,
            media_url,
            dest_path,
            max_size,
            result.headers,
            timeout,
            downloaded_so_far=total_downloaded,
            cancel_token=cancel_token,
            progress=progress,
            loop=loop,
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
