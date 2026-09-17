"""ytmp3.gl extraction provider for YouTube (gamma.gammacloud.net flow)."""

from __future__ import annotations

import asyncio
import logging
import time

import requests

from media_bot_v2.engines.youtube import extract_video_id, matches_youtube_url
from media_bot_v2.providers.base import BaseProvider, ProviderFetchError, ProviderResult

logger = logging.getLogger(__name__)

DEFAULT_API_KEY = "9b0ed5dab31616027ad7154140b0272d"
REFERER_HEADER = "https://ytmp3.gl/"


class YTmp3Provider(BaseProvider):
    """ytmp3.gl provider via gamma.gammacloud.net 4-step conversion flow."""

    name = "ytmp3"
    supported_platforms = ("youtube",)

    def __init__(
        self,
        *,
        api_key: str = DEFAULT_API_KEY,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.api_key = api_key

    def matches(self, url: str) -> bool:
        return matches_youtube_url(url)

    async def fetch(self, url: str) -> ProviderResult:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> ProviderResult:
        video_id = extract_video_id(url)
        if not video_id:
            raise ProviderFetchError(f"Could not extract valid YouTube video ID from {url}")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Referer": REFERER_HEADER,
        }

        # Step 1: Authorization
        ts = int(time.time() * 1000)
        auth_url = f"https://gamma.gammacloud.net/api/v1/auth?api_key={self.api_key}&_={ts}"
        try:
            auth_resp = requests.get(auth_url, headers=headers, timeout=self.timeout)
            auth_resp.raise_for_status()
            auth_data = auth_resp.json()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"ytmp3 auth request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderFetchError("ytmp3 auth returned invalid JSON") from exc

        if auth_data.get("error", 0) > 0:
            raise ProviderFetchError(f"ytmp3 auth returned error: {auth_data.get('error')}")

        bearer_key = auth_data.get("key")
        if not bearer_key:
            raise ProviderFetchError("ytmp3 auth returned no session key")

        # Step 2: Initialize
        ts = int(time.time() * 1000)
        init_headers = dict(headers)
        init_headers["Authorization"] = f"Bearer {bearer_key}"
        init_url = f"https://gamma.gammacloud.net/api/v1/init?_={ts}"

        try:
            init_resp = requests.get(init_url, headers=init_headers, timeout=self.timeout)
            init_resp.raise_for_status()
            init_data = init_resp.json()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"ytmp3 init request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderFetchError("ytmp3 init returned invalid JSON") from exc

        if init_data.get("error", 0) > 0:
            raise ProviderFetchError(f"ytmp3 init returned error: {init_data.get('error')}")

        convert_url = init_data.get("convertURL")
        if not convert_url:
            raise ProviderFetchError("ytmp3 init returned no convertURL")

        # Step 3: Convert
        ts = int(time.time() * 1000)
        # Strip existing v parameter if present
        if "&v=" in convert_url:
            convert_url = convert_url.split("&v=")[0]
        convert_req_url = f"{convert_url}&v={video_id}&f=mp4&_={ts}"

        try:
            conv_resp = requests.get(convert_req_url, headers=headers, timeout=self.timeout)
            conv_resp.raise_for_status()
            conv_data = conv_resp.json()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"ytmp3 convert request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderFetchError("ytmp3 convert returned invalid JSON") from exc

        err_code = conv_data.get("error", 0)
        if err_code > 0:
            # ytmp3 refuses commercial music / copyright protected content
            raise ProviderFetchError(
                f"ytmp3 conversion error {err_code} (commercial music or copyright restriction)"
            )

        if conv_data.get("redirect") == 1 and conv_data.get("redirectURL"):
            redirect_url = conv_data["redirectURL"]
            ts = int(time.time() * 1000)
            conv_resp = requests.get(
                f"{redirect_url}&v={video_id}&f=mp4&_={ts}",
                headers=headers,
                timeout=self.timeout,
            )
            conv_resp.raise_for_status()
            conv_data = conv_resp.json()

        download_url = conv_data.get("downloadURL")
        title = conv_data.get("title") or "YouTube video"

        # Step 4: Progress polling if needed
        progress_url = conv_data.get("progressURL")
        if not download_url and progress_url:
            for _ in range(5):
                time.sleep(1.0)
                ts = int(time.time() * 1000)
                poll_resp = requests.get(f"{progress_url}&_={ts}", headers=headers, timeout=self.timeout)
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
                if poll_data.get("error", 0) > 0:
                    raise ProviderFetchError(f"ytmp3 progress error {poll_data.get('error')}")
                if poll_data.get("title"):
                    title = poll_data["title"]
                if poll_data.get("status") == "download" or poll_data.get("progress", 0) >= 3:
                    download_url = poll_data.get("downloadURL")
                    break

        if not download_url:
            raise ProviderFetchError("ytmp3 did not return a valid downloadURL")

        # Match download script: append &v=...&f=mp4&r=ytmp3.gl
        separator = "&" if "?" in download_url else "?"
        final_download_url = f"{download_url}{separator}v={video_id}&f=mp4&r=ytmp3.gl"

        return ProviderResult(
            provider=self.name,
            media_urls=[final_download_url],
            title=title,
            media_type="video",
            headers={"Referer": REFERER_HEADER},
        )
