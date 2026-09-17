"""TikTok download engine backed by extraction providers with yt-dlp local fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import yt_dlp

from media_bot_v2.engines.base import BaseEngine, DownloadResult, DownloadTooLargeError
from media_bot_v2.engines.youtube import _result_from_info
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

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        dest_dir.mkdir(parents=True, exist_ok=True)

        candidates = self._registry.get_providers_for_platform("tiktok")
        ordered_providers = self._health_tracker.order_for("tiktok", candidates)

        for provider in ordered_providers:
            start_time = time.monotonic()
            try:
                logger.info("Attempting TikTok provider %s for %s", provider.name, url)
                res = await provider.fetch(url)
                dl_result = await download_provider_media(
                    res,
                    dest_dir=dest_dir,
                    max_size=self._max_download_size,
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

        # All providers failed or were suppressed; try local yt-dlp engine
        logger.info("All TikTok providers failed for %s, falling back to local yt-dlp", url)
        try:
            return await asyncio.to_thread(self._download_local_sync, url, dest_dir)
        except DownloadTooLargeError:
            raise
        except Exception as exc:
            logger.warning("Local TikTok engine failed for %s: %s", url, exc)
            raise TikTokDownloadError(texts.DOWNLOAD_FAILED) from exc

    def _download_local_sync(self, url: str, dest_dir: Path) -> DownloadResult:
        ydl_opts = {
            "outtmpl": str(dest_dir / "%(title).150s [%(id)s].%(ext)s"),
            "max_filesize": self._max_download_size,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        if self._cookies_file:
            ydl_opts["cookiefile"] = self._cookies_file

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return _result_from_info(info)
