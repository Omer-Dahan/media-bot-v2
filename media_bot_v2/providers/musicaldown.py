"""musicaldown.com extraction provider for TikTok."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from html.parser import HTMLParser

import requests

from media_bot_v2.providers.base import BaseProvider, ProviderFetchError, ProviderResult
from media_bot_v2.providers.tikwm import matches_tiktok_url

logger = logging.getLogger(__name__)


class _FormInputParser(HTMLParser):
    """Extract form action, hidden inputs, and text/url input field names."""

    def __init__(self) -> None:
        super().__init__()
        self.hidden_inputs: dict[str, str] = {}
        self.text_input_name: str | None = None
        self.form_action: str = "/download"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k.lower(): v for k, v in attrs if v is not None}
        if tag == "form":
            action = attr_dict.get("action")
            if action:
                self.form_action = action
        elif tag == "input":
            name = attr_dict.get("name")
            input_type = attr_dict.get("type", "text").lower()
            val = attr_dict.get("value", "")
            if not name:
                return
            if input_type == "hidden":
                self.hidden_inputs[name] = val
            elif input_type in ("text", "url", "search"):
                self.text_input_name = name


class MusicalDownProvider(BaseProvider):
    """musicaldown.com provider with rotating form field handling."""

    name = "musicaldown"
    supported_platforms = ("tiktok",)

    def matches(self, url: str) -> bool:
        return matches_tiktok_url(url)

    async def fetch(self, url: str) -> ProviderResult:
        return await asyncio.to_thread(self._fetch_sync, url)

    def _fetch_sync(self, url: str) -> ProviderResult:
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        # Step 1: GET homepage to obtain session cookies and parse dynamic form fields
        try:
            get_resp = session.get("https://musicaldown.com/en", timeout=self.timeout)
            get_resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"musicaldown GET page failed: {exc}") from exc

        parser = _FormInputParser()
        parser.feed(get_resp.text)

        if not parser.text_input_name:
            # Fallback regex for input with placeholder or name
            match = re.search(r'<input[^>]+name=["\']([^"\']+)["\'][^>]*placeholder', get_resp.text, re.IGNORECASE)
            if match:
                parser.text_input_name = match.group(1)

        if not parser.text_input_name:
            raise ProviderFetchError("musicaldown failed to locate dynamic URL input field name")

        # Step 2: Build POST body with hidden token fields and target URL
        post_data = dict(parser.hidden_inputs)
        post_data[parser.text_input_name] = url

        post_url = parser.form_action
        if post_url.startswith("/"):
            post_url = f"https://musicaldown.com{post_url}"

        post_headers = {
            "Referer": "https://musicaldown.com/en",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            post_resp = session.post(
                post_url,
                data=post_data,
                headers=post_headers,
                timeout=self.timeout,
            )
            post_resp.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderFetchError(f"musicaldown POST submission failed: {exc}") from exc

        html_body = post_resp.text

        # Step 3: Extract download links
        download_links = re.findall(
            r'<a[^>]+href=["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>',
            html_body,
            re.DOTALL | re.IGNORECASE,
        )

        chosen_url = None
        for link_url, link_text in download_links:
            if "muscdn.app" in link_url or "fastdl" in link_url:
                chosen_url = link_url
                break

        if not chosen_url:
            # Look for any muscdn or direct download href
            bare_matches = re.findall(r'href=["\'](https?://[^"\']*muscdn\.app[^"\']*)["\']', html_body)
            if bare_matches:
                chosen_url = bare_matches[0]

        if not chosen_url:
            raise ProviderFetchError("musicaldown response contained no valid download links")

        # Extract title if present
        title = "TikTok video"
        title_match = re.search(r'<(?:h2|h3|p)[^>]*class=["\'][^"\']*video-desc[^"\']*["\'][^>]*>(.*?)</', html_body, re.IGNORECASE)
        if title_match:
            raw_title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
            if raw_title:
                title = html.unescape(raw_title)

        return ProviderResult(
            provider=self.name,
            media_urls=[chosen_url],
            title=title,
            media_type="video",
        )
