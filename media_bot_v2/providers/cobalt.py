"""Cobalt extraction provider for configured Cobalt instances."""

from __future__ import annotations

import asyncio
import logging

import requests

from media_bot_v2.engines.youtube import matches_youtube_url
from media_bot_v2.providers.base import (
    BaseProvider,
    ProviderFetchError,
    ProviderResult,
    ProviderUnavailableError,
)
from media_bot_v2.providers.tikwm import matches_tiktok_url

logger = logging.getLogger(__name__)


class CobaltProvider(BaseProvider):
    """Cobalt API provider (v10/v7 protocol) against a configured instance."""

    name = "cobalt"
    supported_platforms = ("youtube", "tiktok")

    def __init__(
        self,
        *,
        instance_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.instance_url = instance_url.strip().rstrip("/") if instance_url else None

    @property
    def is_configured(self) -> bool:
        return bool(self.instance_url)

    def matches(self, url: str) -> bool:
        if not self.is_configured:
            return False
        return matches_youtube_url(url) or matches_tiktok_url(url)

    async def fetch(self, url: str) -> ProviderResult:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> ProviderResult:
        if not self.instance_url:
            raise ProviderUnavailableError("Cobalt instance URL is not configured")

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "media-bot-v2/0.1.0",
        }
        payload = {"url": url}

        # Try the configured URL as-is first; a bare base like
        # https://cobalt.domain falls back to /api/json below on a 404.
        endpoint = self.instance_url

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            # If 404 on base endpoint, try with /api/json
            if response.status_code == 404 and not endpoint.endswith("/api/json"):
                endpoint = f"{self.instance_url}/api/json"
                response = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"Cobalt request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderFetchError("Cobalt returned invalid JSON") from exc

        status = data.get("status")

        if status in ("tunnel", "redirect", "stream"):
            media_url = data.get("url")
            if not media_url:
                raise ProviderFetchError("Cobalt response missing stream URL")
            filename = data.get("filename") or "media"
            return ProviderResult(
                provider=self.name,
                media_urls=[media_url],
                title=filename,
                media_type="video",
            )

        if status == "picker":
            picker_items = data.get("picker", [])
            urls = [item["url"] for item in picker_items if isinstance(item, dict) and "url" in item]
            if not urls:
                raise ProviderFetchError("Cobalt picker returned no media items")
            return ProviderResult(
                provider=self.name,
                media_urls=urls,
                title="Cobalt Media",
                media_type="photo",
            )

        if status == "error":
            error_info = data.get("error", {})
            code = error_info.get("code") if isinstance(error_info, dict) else str(error_info)
            raise ProviderFetchError(f"Cobalt instance error: {code}")

        # Fallback if URL is present without explicit status
        if "url" in data:
            return ProviderResult(
                provider=self.name,
                media_urls=[data["url"]],
                title=data.get("filename") or "media",
                media_type="video",
            )

        raise ProviderFetchError(f"Unexpected Cobalt response: {data}")
