"""Engine interface placeholder.

Real engines (YouTube/TikTok/Instagram/direct) are implemented starting at
milestone M2 - see spec/SPEC.md. This file only defines the shape so the
router and tests have something concrete to import in M1.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DownloadResult:
    file_paths: list[str]
    title: str | None = None
    description: str | None = None


class BaseEngine(ABC):
    """Shared contract every platform engine must implement."""

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True if this engine should handle the given URL."""

    @abstractmethod
    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        """Download the media into dest_dir, return local file paths + metadata."""
