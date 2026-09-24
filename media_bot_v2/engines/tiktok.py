"""TikTok download engine backed by extraction providers with yt-dlp local fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import yt_dlp

from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    RouteAttemptTracker,
    UnsupportedUrlError,
)
from media_bot_v2.engines.youtube import (
    _DownloadCancelledSignal,
    _result_from_info,
    summarize_provider_failure,
    summarize_ytdlp_failure,
)
from media_bot_v2.providers.downloader import download_provider_media
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.registry import ProviderRegistry
from media_bot_v2.providers.tikwm import matches_tiktok_url
from media_bot_v2.telegram import texts

logger = logging.getLogger(__name__)


class TikTokDownloadError(Exception):
    """Raised with a classified, Hebrew user-facing error message."""


class TikTokEngine(BaseEngine):
    """TikTok engine trying configured external providers first, falling back to local yt-dlp."""

    name: str = "tiktok"
    supported_platforms: tuple[str, ...] = ("tiktok",)

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        health_tracker: ProviderHealthTracker,
        max_download_size: int,
        cookies_file: str | None = None,
        progress=None,
    ) -> None:
        self._registry = registry
        self._health_tracker = health_tracker
        self._max_download_size = max_download_size
        self._cookies_file = cookies_file
        self._progress = progress

    def matches(self, url: str) -> bool:
        return matches_tiktok_url(url)

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

        candidates = self._registry.get_providers_for_platform("tiktok")
        ordered_providers = self._health_tracker.order_for("tiktok", candidates)

        tracker = RouteAttemptTracker()
        attempted_providers: set[str] = set()

        for provider in ordered_providers:
            if cancel_token is not None and cancel_token.is_set():
                break
            if not provider.matches(url):
                logger.info("Skipping TikTok provider %s for %s: URL capability not supported", provider.name, url)
                continue
            if provider.name.lower() in attempted_providers:
                continue
            attempted_providers.add(provider.name.lower())

            start_time = time.monotonic()
            try:
                logger.info("Attempting TikTok provider %s for %s", provider.name, url)
                res = await provider.fetch(url)
                dl_result = await download_provider_media(
                    res,
                    dest_dir=dest_dir,
                    max_size=self._max_download_size,
                    cancel_token=cancel_token,
                )
                elapsed = time.monotonic() - start_time
                self._health_tracker.record_success(provider.name, "tiktok", elapsed)
                logger.info(
                    "TikTok provider %s succeeded in %.2fs for %s",
                    provider.name,
                    elapsed,
                    url,
                )
                return dl_result
            except DownloadTooLargeError:
                raise
            except Exception as exc:  # noqa: BLE001 - any provider failure must fall through to next candidate
                elapsed = time.monotonic() - start_time
                logger.warning(
                    "TikTok provider %s failed for %s (took %.2fs): %s",
                    provider.name,
                    url,
                    elapsed,
                    exc,
                )
                self._health_tracker.record_failure(provider.name, "tiktok", str(exc))
                tracker.record(provider.name, summarize_provider_failure(exc))

        if cancel_token is not None and cancel_token.is_set():
            return DownloadResult(file_paths=[])

        # All providers failed or were suppressed; try local yt-dlp engine
        logger.info("All TikTok providers failed for %s, falling back to local yt-dlp", url)
        try:
            return await asyncio.to_thread(self._download_local_sync, url, dest_dir, cancel_token)
        except DownloadTooLargeError:
            raise
        except Exception as exc:
            logger.warning("Local TikTok engine failed for %s: %s", url, exc)
            tracker.record("מנוע מקומי (yt-dlp)", summarize_ytdlp_failure(exc))
            raise TikTokDownloadError(tracker.format_summary()) from exc

    def _download_local_sync(
        self,
        url: str,
        dest_dir: Path,
        cancel_token: CancellationToken | None = None,
    ) -> DownloadResult:
        if cancel_token is not None and cancel_token.is_set():
            return DownloadResult(file_paths=[])

        def hook(d: dict) -> None:
            if cancel_token is not None and cancel_token.is_set():
                raise _DownloadCancelledSignal("Download cancelled by timeout budget")

        ydl_opts = {
            "outtmpl": str(dest_dir / "%(title).150s [%(id)s].%(ext)s"),
            "max_filesize": self._max_download_size,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "progress_hooks": [hook],
        }
        if self._cookies_file:
            ydl_opts["cookiefile"] = self._cookies_file

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return _result_from_info(info)
        except _DownloadCancelledSignal:
            logger.info("TikTok download cancelled by timeout budget")
            return DownloadResult(file_paths=[])
        except yt_dlp.utils.DownloadError as exc:
            if isinstance(exc.__cause__, _DownloadCancelledSignal):
                logger.info("TikTok download cancelled by timeout budget")
                return DownloadResult(file_paths=[])
            raise
