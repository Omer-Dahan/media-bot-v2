"""Unit tests for ProviderRegistry."""

from media_bot_v2.config import Settings
from media_bot_v2.providers.base import BaseProvider, ProviderResult
from media_bot_v2.providers.cobalt import CobaltProvider
from media_bot_v2.providers.registry import ProviderRegistry, build_provider_registry
from media_bot_v2.providers.ytmp3 import YTmp3Provider


class _DummyProvider(BaseProvider):
    def __init__(self, name: str, platforms=("tiktok",)):
        super().__init__()
        self.name = name
        self.supported_platforms = platforms

    def matches(self, url: str) -> bool:
        return True

    async def fetch(self, url: str) -> ProviderResult:
        return ProviderResult(provider=self.name, media_urls=["http://example.com/test.mp4"])


def test_registry_order_and_filtering():
    registry = ProviderRegistry(
        tiktok_order=["p2", "p1", "p3"],
        disabled=["p3"],
    )
    p1 = _DummyProvider("p1")
    p2 = _DummyProvider("p2")
    p3 = _DummyProvider("p3")

    registry.register(p1)
    registry.register(p2)
    registry.register(p3)

    providers = registry.get_providers_for_platform("tiktok")
    # p2 first, then p1; p3 is disabled
    assert [p.name for p in providers] == ["p2", "p1"]


def test_registry_skips_unconfigured_cobalt():
    registry = ProviderRegistry(
        youtube_order=["ytmp3", "cobalt"],
    )
    ytmp3 = _DummyProvider("ytmp3")
    cobalt_unconfigured = CobaltProvider(instance_url=None)

    registry.register(ytmp3)
    registry.register(cobalt_unconfigured)

    providers = registry.get_providers_for_platform("youtube")
    # cobalt is unconfigured, so only ytmp3 is returned
    assert [p.name for p in providers] == ["ytmp3"]


def test_registry_includes_configured_cobalt():
    registry = ProviderRegistry(
        youtube_order=["ytmp3", "cobalt"],
    )
    ytmp3 = _DummyProvider("ytmp3")
    cobalt_configured = CobaltProvider(instance_url="https://cobalt.internal")

    registry.register(ytmp3)
    registry.register(cobalt_configured)

    providers = registry.get_providers_for_platform("youtube")
    assert [p.name for p in providers] == ["ytmp3", "cobalt"]


def test_registry_skips_unconfigured_ytmp3():
    """No API key -> ytmp3 never enters the candidate list at all (no
    request, no failure, no cooldown, no log line for a provider that was
    never going to work)."""
    registry = ProviderRegistry(youtube_order=["ytmp3", "cobalt"])
    ytmp3_unconfigured = YTmp3Provider(api_key=None)
    cobalt_configured = CobaltProvider(instance_url="https://cobalt.internal")

    registry.register(ytmp3_unconfigured)
    registry.register(cobalt_configured)

    providers = registry.get_providers_for_platform("youtube")
    assert [p.name for p in providers] == ["cobalt"]


def test_registry_includes_configured_ytmp3():
    registry = ProviderRegistry(youtube_order=["ytmp3", "cobalt"])
    ytmp3_configured = YTmp3Provider(api_key="real-key")
    cobalt_configured = CobaltProvider(instance_url="https://cobalt.internal")

    registry.register(ytmp3_configured)
    registry.register(cobalt_configured)

    providers = registry.get_providers_for_platform("youtube")
    assert [p.name for p in providers] == ["ytmp3", "cobalt"]


def test_build_provider_registry_from_settings(monkeypatch):
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("TIKTOK_PROVIDERS", "musicaldown,tikwm")
    monkeypatch.setenv("DISABLED_PROVIDERS", "tikdownloader")
    monkeypatch.setenv("COBALT_URL", "https://cobalt.example.com")
    monkeypatch.setenv("YTMP3_API_KEY", "test-key")

    settings = Settings(_env_file=None)
    registry = build_provider_registry(settings)

    tiktok_providers = registry.get_providers_for_platform("tiktok")
    assert [p.name for p in tiktok_providers] == ["musicaldown", "tikwm"]

    youtube_providers = registry.get_providers_for_platform("youtube")
    assert [p.name for p in youtube_providers] == ["ytmp3", "cobalt"]


def test_build_provider_registry_skips_ytmp3_without_api_key(monkeypatch):
    """The config-driven wiring, not just the registry primitive: no
    YTMP3_API_KEY in the environment means ytmp3 is built but filtered out of
    every platform's candidate list."""
    monkeypatch.setenv("APP_ID", "1")
    monkeypatch.setenv("APP_HASH", "h")
    monkeypatch.setenv("BOT_TOKEN", "t")
    monkeypatch.setenv("COBALT_URL", "https://cobalt.example.com")
    monkeypatch.delenv("YTMP3_API_KEY", raising=False)

    settings = Settings(_env_file=None)
    assert settings.ytmp3_api_key is None
    registry = build_provider_registry(settings)

    youtube_providers = registry.get_providers_for_platform("youtube")
    assert [p.name for p in youtube_providers] == ["cobalt"]
