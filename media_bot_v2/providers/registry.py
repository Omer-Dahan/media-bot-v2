"""Provider registry managing available providers and per-platform ordering."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from media_bot_v2.config import Settings
from media_bot_v2.providers.base import BaseProvider
from media_bot_v2.providers.cobalt import CobaltProvider
from media_bot_v2.providers.musicaldown import MusicalDownProvider
from media_bot_v2.providers.tikdownloader import TikDownloaderProvider
from media_bot_v2.providers.tikwm import TikWMProvider
from media_bot_v2.providers.ytmp3 import YTmp3Provider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry maintaining active providers and per-platform candidate order."""

    def __init__(
        self,
        *,
        tiktok_order: Iterable[str] = ("tikwm", "tikdownloader", "musicaldown", "cobalt"),
        youtube_order: Iterable[str] = ("ytmp3", "cobalt"),
        disabled: Iterable[str] = (),
    ) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._platform_orders: dict[str, list[str]] = {
            "tiktok": [p.lower() for p in tiktok_order],
            "youtube": [p.lower() for p in youtube_order],
        }
        self._disabled: set[str] = {d.lower() for d in disabled}

    def register(self, provider: BaseProvider) -> None:
        self._providers[provider.name.lower()] = provider

    def get(self, name: str) -> BaseProvider | None:
        return self._providers.get(name.lower())

    def set_platform_order(self, platform: str, order: Iterable[str]) -> None:
        self._platform_orders[platform.lower()] = [p.lower() for p in order]

    def disable_provider(self, name: str) -> None:
        self._disabled.add(name.lower())

    def enable_provider(self, name: str) -> None:
        self._disabled.discard(name.lower())

    def get_providers_for_platform(self, platform: str) -> list[BaseProvider]:
        """Return configured providers for platform in priority order, omitting disabled ones."""
        names = self._platform_orders.get(platform.lower(), [])
        result: list[BaseProvider] = []
        for name in names:
            if name in self._disabled:
                continue
            provider = self._providers.get(name)
            if not provider:
                continue
            # If cobalt is registered but instance_url is not configured, skip it
            if isinstance(provider, CobaltProvider) and not provider.is_configured:
                continue
            result.append(provider)
        return result


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    """Build and populate a ProviderRegistry from typed settings."""
    registry = ProviderRegistry(
        tiktok_order=settings.parsed_tiktok_providers,
        youtube_order=settings.parsed_youtube_providers,
        disabled=settings.parsed_disabled_providers,
    )

    registry.register(TikWMProvider(timeout=settings.provider_timeout))
    registry.register(TikDownloaderProvider(timeout=settings.provider_timeout))
    registry.register(MusicalDownProvider(timeout=settings.provider_timeout))
    registry.register(
        YTmp3Provider(
            api_key=settings.ytmp3_api_key,
            timeout=settings.provider_timeout,
        )
    )
    registry.register(
        CobaltProvider(
            instance_url=settings.cobalt_url,
            timeout=settings.provider_timeout,
        )
    )

    return registry
