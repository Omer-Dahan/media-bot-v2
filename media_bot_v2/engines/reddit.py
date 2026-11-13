"""Reddit video download engine, v1: Reddit video posts (`reddit_video` and
crossposts) only - image galleries, image posts, and links to other
platforms are out of scope and raise `texts.REDDIT_NO_VIDEO`.

This engine only handles post URLs (`reddit.com/.../comments/<id>` or bare
`redd.it/<id>`), per `matches_reddit_url`. Direct media URLs like
`v.redd.it/<id>/DASH_720.mp4` or `i.redd.it/<id>.jpg` stay on `DirectEngine`.

Anonymous access to Reddit's own JSON/API endpoints is blocked from this
host (verified 2026-11-04: `old.reddit.com/.../.json` and
`api.reddit.com/...` both return HTTP 403 from any User-Agent). The OAuth
token endpoint is reachable, so this engine authenticates app-only
(`grant_type=client_credentials`) with a Reddit "script" app's client
id/secret, then fetches the post from `oauth.reddit.com`. With no
credentials configured it raises a clear "not configured" error rather than
attempting anonymous access that is known to fail here.

Video download reuses the existing HLS engine (engines/hls.py) for the
`hls_url` rendition. When only `fallback_url` (video-only mp4) is published,
this engine best-effort probes Reddit's own DASH audio sibling URLs and
muxes them locally with ffmpeg; a video with no retrievable audio is still
delivered silent rather than failed.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
import urllib3.util.connection as urllib3_cn
from requests.auth import HTTPBasicAuth

from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.hls import HlsDownloadError, HlsUnsupportedError, download_hls
from media_bot_v2.engines.ssrf_guard import SSRFBlockedError, safe_request
from media_bot_v2.engines.ytdlp_support import rename_to_safe_display_name
from media_bot_v2.executor import run_in_thread
from media_bot_v2.telegram import texts

logger = logging.getLogger(__name__)

REDDIT_HOSTS = ("reddit.com", "redd.it")

_DEFAULT_USER_AGENT = "linux:media-bot-v2:1.0 (by /u/media_bot)"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_TOKEN_REFRESH_MARGIN_SECONDS = 60
_REQUEST_TIMEOUT = 15
_CHUNK_SIZE = 1024 * 1024
_FFMPEG_MUX_TIMEOUT = 180.0

_REDDIT_SUB_COMMENTS_RE = re.compile(r"^/r/[^/]+/comments/([0-9a-zA-Z]+)(?:/.*)?$")
_REDDIT_BARE_COMMENTS_RE = re.compile(r"^/comments/([0-9a-zA-Z]+)(?:/.*)?$")
_REDDIT_SHORT_ID_RE = re.compile(r"^/([0-9a-zA-Z]+)/?$")


class RedditDownloadError(Exception):
    """Raised with an already-classified, Hebrew, user-facing message."""


class _RedditCancelled(Exception):
    """Internal signal: cancellation observed mid-download."""


def _reddit_host_kind(host: str) -> str | None:
    host = host.lower()
    if host == "reddit.com" or host.endswith(".reddit.com"):
        return "reddit.com"
    if host == "redd.it" or host.endswith(".redd.it"):
        return "redd.it"
    return None


def matches_reddit_url(url: str) -> bool:
    """`reddit.com` (and subdomains) needs a `/r/<sub>/comments/<id>` or
    `/comments/<id>` post path; `redd.it` (and `v.redd.it`) needs a bare
    `/<id>` path. Listing pages (`/r/sub`), `/user/...` and `/search` never
    match either shape."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.netloc or "").split(":")[0]
    kind = _reddit_host_kind(host)
    if kind is None:
        return False
    path = parsed.path or "/"
    if kind == "reddit.com":
        return bool(_REDDIT_SUB_COMMENTS_RE.match(path) or _REDDIT_BARE_COMMENTS_RE.match(path))
    return bool(_REDDIT_SHORT_ID_RE.match(path))


def extract_reddit_id(url: str) -> str | None:
    """The base36 post id, for cache keys."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").split(":")[0]
    kind = _reddit_host_kind(host)
    if kind is None:
        return None
    path = parsed.path or "/"
    if kind == "reddit.com":
        match = _REDDIT_SUB_COMMENTS_RE.match(path) or _REDDIT_BARE_COMMENTS_RE.match(path)
    else:
        match = _REDDIT_SHORT_ID_RE.match(path)
    return match.group(1) if match else None


@contextmanager
def _maybe_force_ipv4(force_ipv4: bool):
    """Same best-effort technique instagram.py/youtube.py apply for yt-dlp
    (`source_address`), adapted for `requests`: urllib3 only offers this via
    its `allowed_gai_family` hook, so it is monkeypatched for the duration
    of the one request this wraps."""
    if not force_ipv4:
        yield
        return
    original = urllib3_cn.allowed_gai_family
    urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
    try:
        yield
    finally:
        urllib3_cn.allowed_gai_family = original


_token_lock = threading.Lock()
_cached_token: str | None = None
_cached_token_expiry: float = 0.0


def _fetch_access_token(
    client_id: str, client_secret: str, user_agent: str, timeout: float
) -> tuple[str, float]:
    try:
        response = requests.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=HTTPBasicAuth(client_id, client_secret),
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RedditDownloadError(texts.REDDIT_NETWORK_ERROR) from exc
    if response.status_code in (401, 403):
        raise RedditDownloadError(texts.REDDIT_AUTH_FAILED)
    try:
        response.raise_for_status()
        payload = response.json()
        return payload["access_token"], float(payload.get("expires_in", 3600))
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise RedditDownloadError(texts.REDDIT_AUTH_FAILED) from exc


def _get_access_token(client_id: str, client_secret: str, user_agent: str, timeout: float) -> str:
    """One module-level cached token, refreshed once less than
    `_TOKEN_REFRESH_MARGIN_SECONDS` remain - shared across every
    `RedditEngine` instance in this process."""
    global _cached_token, _cached_token_expiry
    with _token_lock:
        now = time.monotonic()
        if _cached_token is not None and now < _cached_token_expiry - _TOKEN_REFRESH_MARGIN_SECONDS:
            return _cached_token
        token, expires_in = _fetch_access_token(client_id, client_secret, user_agent, timeout)
        _cached_token = token
        _cached_token_expiry = now + expires_in
        return token


def _fetch_post(post_id: str, token: str, user_agent: str, timeout: float, force_ipv4: bool) -> dict:
    url = f"https://oauth.reddit.com/comments/{post_id}?raw_json=1&limit=1"
    headers = {"Authorization": f"bearer {token}", "User-Agent": user_agent}
    try:
        with _maybe_force_ipv4(force_ipv4):
            response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RedditDownloadError(texts.REDDIT_NETWORK_ERROR) from exc
    status = response.status_code
    if status in (401, 403):
        raise RedditDownloadError(texts.REDDIT_PRIVATE_OR_LOGIN)
    if status == 404:
        raise RedditDownloadError(texts.REDDIT_NOT_FOUND)
    if status == 429:
        raise RedditDownloadError(texts.REDDIT_RATE_LIMITED)
    if status >= 400:
        raise RedditDownloadError(texts.REDDIT_NETWORK_ERROR)
    try:
        payload = response.json()
        return payload[0]["data"]["children"][0]["data"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RedditDownloadError(texts.REDDIT_NETWORK_ERROR) from exc


class RedditEngine(BaseEngine):
    """Reddit video engine using app-only OAuth + the existing HLS engine."""

    name: str = "reddit"
    supported_platforms: tuple[str, ...] = ("reddit",)

    def __init__(
        self,
        *,
        max_download_size: int,
        progress=None,
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
        max_height: int = 720,
        force_ipv4: bool = False,
    ) -> None:
        self._max_download_size = max_download_size
        self._progress = progress
        self._client_id = client_id or os.getenv("REDDIT_CLIENT_ID")
        self._client_secret = client_secret or os.getenv("REDDIT_CLIENT_SECRET")
        self._user_agent = user_agent or os.getenv("REDDIT_USER_AGENT") or _DEFAULT_USER_AGENT
        self._max_height = max_height
        self._force_ipv4 = force_ipv4

    def matches(self, url: str) -> bool:
        return matches_reddit_url(url)

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
        try:
            return await run_in_thread(self._download_sync, url, dest_dir, cancel_token)
        except (RedditDownloadError, DownloadTooLargeError, UnsupportedUrlError):
            raise
        except Exception as exc:
            logger.exception("Unexpected error in Reddit download for %s", url)
            raise RedditDownloadError(texts.REDDIT_GENERIC_FAILURE) from exc

    def _download_sync(
        self, url: str, dest_dir: Path, cancel_token: CancellationToken | None
    ) -> DownloadResult:
        if not self._client_id or not self._client_secret:
            raise RedditDownloadError(texts.REDDIT_NOT_CONFIGURED)
        post_id = extract_reddit_id(url)
        if not post_id:
            raise UnsupportedUrlError(texts.UNSUPPORTED_URL)

        try:
            token = _get_access_token(self._client_id, self._client_secret, self._user_agent, _REQUEST_TIMEOUT)
            post = _fetch_post(post_id, token, self._user_agent, _REQUEST_TIMEOUT, self._force_ipv4)

            title = post.get("title")
            media_post = post
            crosspost_list = post.get("crosspost_parent_list")
            if isinstance(crosspost_list, list) and crosspost_list:
                media_post = crosspost_list[0]

            video = (media_post.get("secure_media") or {}).get("reddit_video") or (
                media_post.get("media") or {}
            ).get("reddit_video")
            if not video:
                raise RedditDownloadError(texts.REDDIT_NO_VIDEO)

            if cancel_token is not None and cancel_token.is_set():
                return DownloadResult(file_paths=[])

            hls_url = video.get("hls_url")
            if hls_url:
                result_path = self._download_via_hls(hls_url, dest_dir, cancel_token)
            else:
                fallback_url = video.get("fallback_url")
                if not fallback_url:
                    raise RedditDownloadError(texts.REDDIT_NO_VIDEO)
                result_path = self._download_fallback(fallback_url, post_id, dest_dir, cancel_token)
        except _RedditCancelled:
            return DownloadResult(file_paths=[])

        if cancel_token is not None and cancel_token.is_set():
            result_path.unlink(missing_ok=True)
            return DownloadResult(file_paths=[])

        final_path = self._rename_result(result_path, post_id, title)
        return DownloadResult(file_paths=[str(final_path)], title=title)

    def _download_via_hls(
        self, hls_url: str, dest_dir: Path, cancel_token: CancellationToken | None
    ) -> Path:
        try:
            result = download_hls(
                hls_url,
                dest_dir,
                max_size=self._max_download_size,
                max_height=self._max_height,
                cancel_token=cancel_token,
            )
        except HlsUnsupportedError as exc:
            logger.warning("Reddit HLS input unsupported (%s): %s", exc.kind, exc)
            raise RedditDownloadError(texts.hls_failure_text(exc.kind)) from exc
        except DownloadTooLargeError as exc:
            raise DownloadTooLargeError(texts.hls_failure_text("size_cap")) from exc
        except HlsDownloadError as exc:
            logger.warning("Reddit HLS transient failure for %s: %s", hls_url, exc)
            raise RedditDownloadError(texts.REDDIT_NETWORK_ERROR) from exc
        if not result.file_paths:
            raise RedditDownloadError(texts.REDDIT_NO_VIDEO)
        return Path(result.file_paths[0])

    def _download_fallback(
        self, fallback_url: str, post_id: str, dest_dir: Path, cancel_token: CancellationToken | None
    ) -> Path:
        video_path = dest_dir / f"{post_id}-video.mp4"
        try:
            self._stream_download(fallback_url, video_path, cancel_token)
        except SSRFBlockedError as exc:
            raise UnsupportedUrlError(texts.UNSUPPORTED_URL) from exc

        media_id = self._media_id_from_url(fallback_url)
        audio_path = dest_dir / f"{post_id}-audio.mp4"
        audio_downloaded = False
        if media_id is not None:
            for audio_name in ("DASH_AUDIO_128.mp4", "DASH_Audio_128.mp4"):
                audio_url = f"https://v.redd.it/{media_id}/{audio_name}"
                if not self._probe_audio_url(audio_url):
                    continue
                try:
                    self._stream_download(audio_url, audio_path, cancel_token)
                    audio_downloaded = True
                except _RedditCancelled:
                    raise
                except (SSRFBlockedError, requests.RequestException, OSError) as exc:
                    logger.debug("Reddit audio sibling download failed for %s: %s", audio_url, exc)
                    audio_path.unlink(missing_ok=True)
                break

        if not audio_downloaded:
            return video_path

        muxed_path = dest_dir / f"reddit-{post_id}.mp4"
        try:
            self._mux_audio_video(video_path, audio_path, muxed_path)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("Reddit ffmpeg mux failed, delivering silent video instead: %s", exc)
            muxed_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            return video_path
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        return muxed_path

    @staticmethod
    def _media_id_from_url(url: str) -> str | None:
        try:
            parts = [p for p in urlparse(url).path.split("/") if p]
        except ValueError:
            return None
        return parts[0] if parts else None

    def _probe_audio_url(self, audio_url: str) -> bool:
        try:
            with safe_request("HEAD", audio_url, timeout=_REQUEST_TIMEOUT) as response:
                if response.status_code != 200:
                    return False
                content_type = (response.headers.get("Content-Type") or "").lower()
                return content_type.startswith(("audio/", "video/", "application/octet-stream"))
        except (SSRFBlockedError, requests.RequestException):
            return False

    def _stream_download(
        self, url: str, dest_path: Path, cancel_token: CancellationToken | None
    ) -> None:
        total = 0
        with safe_request("GET", url, stream=True, timeout=_REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            try:
                with open(dest_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                        if cancel_token is not None and cancel_token.is_set():
                            raise _RedditCancelled()
                        if not chunk:
                            continue
                        total += len(chunk)
                        if self._max_download_size is not None and total > self._max_download_size:
                            raise DownloadTooLargeError(
                                texts.format_download_too_large(total, self._max_download_size),
                                file_size=total,
                                max_size=self._max_download_size,
                                url=url,
                            )
                        f.write(chunk)
            except Exception:
                dest_path.unlink(missing_ok=True)
                raise

    @staticmethod
    def _mux_audio_video(video_path: Path, audio_path: Path, output_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=_FFMPEG_MUX_TIMEOUT, check=True
        )

    @staticmethod
    def _rename_result(path: Path, post_id: str, title: str | None) -> Path:
        id_path = path.with_name(f"{post_id}{path.suffix}")
        if path != id_path:
            path.rename(id_path)
        renamed = rename_to_safe_display_name({"id": post_id, "title": title or "reddit"}, str(id_path))
        return Path(renamed)
