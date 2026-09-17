"""Base interface for external extraction providers (TikWM, musicaldown, etc.).

Providers fetch direct media URLs and metadata without downloading full media
files to disk. The pipeline then streams the resulting direct URL(s) to disk.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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

        Runs network requests in worker threads via asyncio.to_thread to avoid
        blocking the event loop.
        """
