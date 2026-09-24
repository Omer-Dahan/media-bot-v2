"""TikWM extraction provider for TikTok."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import requests

from media_bot_v2.providers.base import BaseProvider, ProviderFetchError, ProviderResult

logger = logging.getLogger(__name__)

TIKTOK_HOSTS = ("tiktok.com", "douyin.com")


def matches_tiktok_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.split(":")[0].lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in TIKTOK_HOSTS)


class TikWMProvider(BaseProvider):
    """TikWM API provider (https://www.tikwm.com/api/)."""

    name = "tikwm"
    supported_platforms = ("tiktok",)

    def matches(self, url: str) -> bool:
        return matches_tiktok_url(url)

    async def fetch(self, url: str) -> ProviderResult:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> ProviderResult:
        endpoint = "https://www.tikwm.com/api/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            )
        }
        params = {"url": url, "hd": "1"}

        try:
            response = requests.get(
                endpoint, params=params, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"TikWM network request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderFetchError("TikWM returned invalid JSON response") from exc

        code = payload.get("code")
        if code != 0:
            msg = payload.get("msg") or f"Error code {code}"
            raise ProviderFetchError(f"TikWM API error: {msg}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderFetchError("TikWM response missing data object")

        title = data.get("title") or "TikTok video"

        # Check for photo slideshow
        images = data.get("images")
        if isinstance(images, list) and images:
            valid_images = [img for img in images if isinstance(img, str) and img.strip()]
            if valid_images:
                return ProviderResult(
                    provider=self.name,
                    media_urls=valid_images,
                    title=title,
                    media_type="photo",
                )

        # Video: prefer hdplay, then play, then wmplay
        play_url = data.get("hdplay") or data.get("play") or data.get("wmplay")
        if not play_url or not isinstance(play_url, str):
            raise ProviderFetchError("TikWM did not return a valid video URL")

        if play_url.startswith("/"):
            play_url = f"https://www.tikwm.com{play_url}"

        size = data.get("hd_size") or data.get("size")
        size_hint = int(size) if size and str(size).isdigit() else None

        return ProviderResult(
            provider=self.name,
            media_urls=[play_url],
            title=title,
            media_type="video",
            size_hint=size_hint,
        )
