"""Base interface for external extraction providers (TikWM, musicaldown, etc.).

Providers fetch direct media URLs and metadata without downloading full media
files to disk. The pipeline then streams the resulting direct URL(s) to disk.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Query-string secrets (api_key=..., token=..., ...) and bearer tokens that
# show up verbatim inside provider exception messages - `requests` embeds the
# full request URL in connection/HTTP errors, so "ytmp3 auth request failed:
# <requests exception>" leaked the ytmp3 API key into logs and the
# provider_health.last_error column at WARNING level (production incident
# 2026-10-01). Anything matching one of these keys gets masked before the
# text reaches a log line or is persisted anywhere.
_SENSITIVE_QUERY_PARAM_RE = re.compile(
    r"(?i)\b(api[_-]?key|key|token|signature|sig|password|auth)=([^&\s\"'<>]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")


def mask_secrets(text: str) -> str:
    """Redact query-string secrets and bearer tokens from free-form text
    (an exception message, a log line, ...) before it is logged or stored.
    Safe to call on text with nothing to mask - returns it unchanged."""
    if not text:
        return text
    masked = _SENSITIVE_QUERY_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    masked = _BEARER_TOKEN_RE.sub("Bearer ***", masked)
    return masked


class ProviderError(Exception):
    """Base exception for provider failures."""


class ProviderFetchError(ProviderError):
    """Raised when a provider fails to extract direct media URLs."""


class ProviderUnavailableError(ProviderFetchError):
    """Raised when a provider is disabled or not configured (e.g. missing cobalt URL)."""


@dataclass
class ProviderResult:
    """Extraction result containing direct media URL(s) and metadata."""

    provider: str
    media_urls: list[str]
    title: str | None = None
    media_type: str = "video"  # "video", "audio", "photo"
    size_hint: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_url(self) -> str:
        if not self.media_urls:
            raise ValueError("ProviderResult contains no media URLs")
        return self.media_urls[0]


class BaseProvider(ABC):
    """Contract for external media extraction providers."""

    name: str
    supported_platforms: tuple[str, ...]

    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True if this provider can handle the given URL."""

    @abstractmethod
    async def fetch(self, url: str) -> ProviderResult:
        """Extract direct media URL(s) and metadata.

        Runs network requests in worker threads via the dedicated thread pool
        (media_bot_v2.executor.run_in_thread) to avoid blocking the event loop.
        """
