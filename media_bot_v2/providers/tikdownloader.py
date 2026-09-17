"""tikdownloader.io extraction provider for TikTok."""

from __future__ import annotations

import asyncio
import html
import logging
import re

import requests

from media_bot_v2.providers.base import BaseProvider, ProviderFetchError, ProviderResult
from media_bot_v2.providers.tikwm import matches_tiktok_url

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(
    r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<(?:h3|h2|p)[^>]*class=[\"'][^\"']*title[^\"']*[\"'][^>]*>(.*?)</", re.IGNORECASE)


class TikDownloaderProvider(BaseProvider):
    """tikdownloader.io AJAX search provider."""

    name = "tikdownloader"
    supported_platforms = ("tiktok",)

    def matches(self, url: str) -> bool:
        return matches_tiktok_url(url)

    async def fetch(self, url: str) -> ProviderResult:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> ProviderResult:
        endpoint = "https://tikdownloader.io/api/ajaxSearch"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://tikdownloader.io/",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        data = {"q": url, "lang": "en"}

        try:
            response = requests.post(
                endpoint, data=data, headers=headers, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"tikdownloader network request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderFetchError("tikdownloader returned invalid JSON") from exc

        if payload.get("status") != "ok":
            raise ProviderFetchError(
                f"tikdownloader returned error status: {payload.get('status')}"
            )

        html_content = payload.get("data")
        if not html_content or not isinstance(html_content, str):
            raise ProviderFetchError("tikdownloader response contained empty HTML")

        # Extract title
        title = "TikTok video"
        title_match = _TITLE_RE.search(html_content)
        if title_match:
            raw_title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
            if raw_title:
                title = html.unescape(raw_title)

        # Extract download links
        links = _LINK_RE.findall(html_content)
        if not links:
            # Fallback regex for bare hrefs containing snapcdn or mp4
            bare_matches = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_content)
            links = [(l, "") for l in bare_matches if "snapcdn.app" in l or ".mp4" in l]

        if not links:
            raise ProviderFetchError("No download links found in tikdownloader response")

        # Prioritize HD / no-watermark links from dl.snapcdn.app
        best_url = None
        for href, text in links:
            lowered_text = text.lower()
            if "snapcdn.app" in href:
                if "hd" in lowered_text or "without watermark" in lowered_text or "no watermark" in lowered_text:
                    best_url = href
                    break
                if best_url is None:
                    best_url = href

        if not best_url:
            best_url = links[0][0]

        return ProviderResult(
            provider=self.name,
            media_urls=[best_url],
            title=title,
            media_type="video",
        )
