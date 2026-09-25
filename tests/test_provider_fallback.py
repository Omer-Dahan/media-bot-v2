"""End-to-end fallback and pipeline tests with mocked providers.

Guaranteed zero network calls - direct media streaming uses a mocked requests.get.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.tiktok import TikTokDownloadError, TikTokEngine
from media_bot_v2.engines.youtube import YouTubeDownloadError, YouTubeEngine
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.providers.base import BaseProvider, ProviderFetchError, ProviderResult
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.registry import ProviderRegistry


class _MockProvider(BaseProvider):
    def __init__(self, name: str, *, fail: bool = False, media_url: str = "http://example.com/media.mp4"):
        super().__init__()
        self.name = name
        self.fail = fail
        self.media_url = media_url
        self.supported_platforms = ("tiktok", "youtube")
        self.fetch_calls: list[str] = []

    def matches(self, url: str) -> bool:
        return True

    async def fetch(self, url: str) -> ProviderResult:
        self.fetch_calls.append(url)
        if self.fail:
            raise ProviderFetchError(f"{self.name} simulated failure")
        return ProviderResult(
            provider=self.name,
            media_urls=[self.media_url],
            title=f"Title by {self.name}",
            media_type="video",
        )


class _FakeUploader:
    def __init__(self):
        self.sent: list[Path] = []
        self.archived: list[object] = []

    async def send_file(self, path: Path, *, caption=None, **kwargs):
        self.sent.append(path)
        return MagicMock(id=len(self.sent))

    async def copy_to_archive(self, message, **kwargs):
        self.archived.append(message)
        return MagicMock(id=999)

    async def send_cached(self, archive_chat: str, message_ids: list[int], **kwargs):
        return MagicMock()


class _FakeProgress:
    def __init__(self):
        self.updates: list[str] = []

    async def update(self, text: str) -> None:
        self.updates.append(text)


def _setup_db_and_services():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(User(user_id=1, free=5, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()

    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=10_000_000)
    health_tracker = ProviderHealthTracker(session_factory, failure_threshold=2, cooldown_seconds=300)
    return session_factory, credits_service, health_tracker


def _mock_streaming_get(content: bytes = b"fake-video-bytes-data"):
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Length": str(len(content))}
    mock_resp.iter_content.return_value = [content]
    mock_resp.raise_for_status.return_value = None
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None
    return mock_resp


async def test_tiktok_fallback_from_failing_provider_to_next_in_line(tmp_path):
    _, _, health_tracker = _setup_db_and_services()

    p1 = _MockProvider("tikwm", fail=True)
    p2 = _MockProvider("tikdownloader", fail=False)

    registry = ProviderRegistry(tiktok_order=["tikwm", "tikdownloader"])
    registry.register(p1)
    registry.register(p2)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )

    with patch("media_bot_v2.providers.downloader.requests.get", return_value=_mock_streaming_get()):
        result = await engine.download("https://www.tiktok.com/@user/video/123", dest_dir=tmp_path)

    # Both providers were called; p1 failed, p2 succeeded
    assert len(p1.fetch_calls) == 1
    assert len(p2.fetch_calls) == 1
    assert result.title == "Title by tikdownloader"
    assert len(result.file_paths) == 1
    assert Path(result.file_paths[0]).exists()

    # Health tracker recorded failure for p1 and success for p2
    candidates = [p1, p2]
    ordered = health_tracker.order_for("tiktok", candidates)
    # p2 has 100% success rate, so it is ordered before p1
    assert [p.name for p in ordered] == ["tikdownloader", "tikwm"]


async def test_tiktok_full_pipeline_with_provider(tmp_path):
    session_factory, credits_service, health_tracker = _setup_db_and_services()
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)

    provider = _MockProvider("tikwm", fail=False)
    registry = ProviderRegistry(tiktok_order=["tikwm"])
    registry.register(provider)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )
    uploader = _FakeUploader()
    progress = _FakeProgress()

    with patch("media_bot_v2.providers.downloader.requests.get", return_value=_mock_streaming_get()):
        await pipeline.run(
            user_id=1,
            url="https://www.tiktok.com/@user/video/123",
            engine=engine,
            uploader=uploader,
            progress=progress,
        )

    assert len(uploader.sent) == 1
    assert len(uploader.archived) == 1
    assert progress.updates[-1] == "הושלם ✅"

    # Verify credit deducted
    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 4

    # Verify local task files deleted
    assert not any(tmp_path.rglob("*.mp4"))


async def test_tiktok_falls_back_to_local_when_all_providers_fail(tmp_path):
    _, _, health_tracker = _setup_db_and_services()

    p1 = _MockProvider("tikwm", fail=True)
    registry = ProviderRegistry(tiktok_order=["tikwm"])
    registry.register(p1)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )

    fake_local_file = tmp_path / "local.mp4"
    fake_local_file.write_bytes(b"local-ytdlp-video")

    with patch.object(engine, "_download_local_sync") as mock_local:
        from media_bot_v2.engines.base import DownloadResult
        mock_local.return_value = DownloadResult(file_paths=[str(fake_local_file)], title="Local TikTok")
        result = await engine.download("https://www.tiktok.com/@user/video/123", dest_dir=tmp_path)

    assert len(p1.fetch_calls) == 1
    mock_local.assert_called_once()
    assert result.title == "Local TikTok"


async def test_tiktok_surfaces_error_when_all_providers_and_local_fail(tmp_path):
    _, _, health_tracker = _setup_db_and_services()

    p1 = _MockProvider("tikwm", fail=True)
    registry = ProviderRegistry(tiktok_order=["tikwm"])
    registry.register(p1)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )

    with (
        patch.object(engine, "_download_local_sync", side_effect=RuntimeError("yt-dlp blocked")),
        pytest.raises(TikTokDownloadError),
    ):
        await engine.download("https://www.tiktok.com/@user/video/123", dest_dir=tmp_path)


async def test_youtube_falls_back_to_provider_when_local_engine_fails(tmp_path):
    _, _, health_tracker = _setup_db_and_services()

    ytmp3 = _MockProvider("ytmp3", fail=False)
    registry = ProviderRegistry(youtube_order=["ytmp3"])
    registry.register(ytmp3)

    yt_engine = YouTubeEngine(
        quality="720",
        max_download_size=100 * 1024 * 1024,
        registry=registry,
        health_tracker=health_tracker,
    )

    # Simulate yt-dlp failure
    with patch.object(
        yt_engine,
        "_download_sync",
        side_effect=YouTubeDownloadError("שגיאת פענוח ביוטיוב: חסר JS runtime"),
    ), patch("media_bot_v2.providers.downloader.requests.get", return_value=_mock_streaming_get()):
        result = await yt_engine.download(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", dest_dir=tmp_path
        )

    assert result.title == "Title by ytmp3"
    assert len(ytmp3.fetch_calls) == 1
