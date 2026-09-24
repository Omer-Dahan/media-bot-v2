"""Regression tests for the 8 lessons learned from the old bot failures.

Covers:
1. Capability pre-filtering: direct links (.zim) do not enter yt-dlp or provider routes.
2. Early size check: HEAD Content-Length check rejects oversized files immediately with exact Hebrew message.
3. Single attempt per provider: failed providers are attempted at most once per request.
4. Attempt summary: failure message includes concise summary of attempted routes and reasons.
5. Accurate diagnostics: JS runtime missing, unsupported URL, and oversized file classifications.
6. Irrelevant routes: skipped routes are logged and never marked as failed.
7. Explicit playlist trimming: message specifies how many items were downloaded out of total.
8. Request time budget: timeout cancels request, reports to user, cleans up files, and charges no credits.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import (
    BaseEngine,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.tiktok import TikTokDownloadError, TikTokEngine
from media_bot_v2.engines.youtube import (
    YouTubeDownloadError,
    YouTubeEngine,
    classify_youtube_error,
)
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.providers.base import BaseProvider, ProviderFetchError, ProviderResult
from media_bot_v2.providers.cobalt import CobaltProvider
from media_bot_v2.providers.health import ProviderHealthTracker
from media_bot_v2.providers.musicaldown import MusicalDownProvider
from media_bot_v2.providers.registry import ProviderRegistry
from media_bot_v2.providers.tikdownloader import TikDownloaderProvider
from media_bot_v2.providers.tikwm import TikWMProvider
from media_bot_v2.providers.ytmp3 import YTmp3Provider
from media_bot_v2.telegram import texts


class _MockProgress:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, text: str) -> None:
        self.updates.append(text)


class _MockUploader:
    def __init__(self) -> None:
        self.sent: list[Path] = []
        self.archived: list[object] = []

    async def send_file(self, path: Path, *, caption: str | None = None) -> MagicMock:
        self.sent.append(path)
        return MagicMock(id=len(self.sent))

    async def forward_to_archive(self, message: object) -> MagicMock:
        self.archived.append(message)
        return MagicMock(id=len(self.archived) + 100)

    async def send_cached(self, archive_chat: str, message_ids: list[int]) -> MagicMock:
        return MagicMock()


class _MockProvider(BaseProvider):
    def __init__(
        self,
        name: str,
        *,
        fail: bool = False,
        error_msg: str = "failed",
        matches_return: bool = True,
    ) -> None:
        super().__init__()
        self.name = name
        self.fail = fail
        self.error_msg = error_msg
        self.matches_return = matches_return
        self.fetch_calls: list[str] = []

    def matches(self, url: str) -> bool:
        return self.matches_return

    async def fetch(self, url: str) -> ProviderResult:
        self.fetch_calls.append(url)
        if self.fail:
            raise ProviderFetchError(self.error_msg)
        return ProviderResult(
            provider=self.name,
            media_urls=["http://example.com/media.mp4"],
            title=f"Title by {self.name}",
        )


def _setup_test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(User(user_id=1, free=5, paid=0, bandwidth_used=0, total_bandwidth=0, is_blocked=0))
        session.commit()
    credits_service = CreditsService(session_factory, enable_vip=True, owner_ids=[], free_bandwidth=10_000_000)
    health_tracker = ProviderHealthTracker(session_factory, failure_threshold=2, cooldown_seconds=300)
    return session_factory, credits_service, health_tracker


# ==============================================================================
# Lesson 1: Direct file link in wrong track & capability pre-filtering
# ==============================================================================


def test_lesson1_zim_link_matches_direct_engine_only():
    """A direct Wikipedia .zim link must match DirectEngine and NOT YouTube/TikTok engines or providers."""
    zim_url = "https://dumps.wikimedia.org/other/kiwix/zim/wikipedia/wikipedia_he_all_maxi_2024-01.zim"

    direct_engine = DirectEngine()
    youtube_engine = YouTubeEngine(quality="720", max_download_size=1000)
    tiktok_engine = TikTokEngine(
        registry=ProviderRegistry(),
        health_tracker=MagicMock(),
        max_download_size=1000,
    )

    assert direct_engine.matches(zim_url) is True
    assert youtube_engine.matches(zim_url) is False
    assert tiktok_engine.matches(zim_url) is False

    # Check external providers as well
    assert TikWMProvider().matches(zim_url) is False
    assert TikDownloaderProvider().matches(zim_url) is False
    assert MusicalDownProvider().matches(zim_url) is False
    assert YTmp3Provider().matches(zim_url) is False
    assert CobaltProvider().matches(zim_url) is False


async def test_lesson1_non_matching_url_rejected_by_engines_without_calling_ytdlp(tmp_path):
    """Calling an engine on an incompatible URL immediately raises UnsupportedUrlError without running yt-dlp."""
    zim_url = "https://dumps.wikimedia.org/other/kiwix/zim/wikipedia.zim"

    yt_engine = YouTubeEngine(quality="720", max_download_size=1000)
    with patch("media_bot_v2.engines.youtube.yt_dlp.YoutubeDL") as mock_ydl:
        with pytest.raises(UnsupportedUrlError) as exc_info:
            await yt_engine.download(zim_url, dest_dir=tmp_path)
        mock_ydl.assert_not_called()
    assert texts.UNSUPPORTED_URL in str(exc_info.value)

    tt_engine = TikTokEngine(
        registry=ProviderRegistry(),
        health_tracker=MagicMock(),
        max_download_size=1000,
    )
    with patch("media_bot_v2.engines.tiktok.yt_dlp.YoutubeDL") as mock_ydl:
        with pytest.raises(UnsupportedUrlError) as exc_info:
            await tt_engine.download(zim_url, dest_dir=tmp_path)
        mock_ydl.assert_not_called()
    assert texts.UNSUPPORTED_URL in str(exc_info.value)


# ==============================================================================
# Lesson 2: Size check before direct download (HEAD / Content-Length)
# ==============================================================================


async def test_lesson2_direct_oversized_zim_rejected_on_head_before_get(tmp_path):
    """A 100GB direct link (.zim) is rejected on preflight HEAD without initiating GET or writing bytes."""
    zim_url = "https://dumps.wikimedia.org/other/kiwix/zim/wikipedia.zim"
    max_size = 4 * 1024 * 1024 * 1024  # 4GB limit

    head_resp = MagicMock()
    head_resp.status_code = 200
    head_resp.headers = {"Content-Length": str(100 * 1024 * 1024 * 1024)}  # 100GB
    head_resp.__enter__.return_value = head_resp
    head_resp.__exit__.return_value = None

    engine = DirectEngine(max_download_size=max_size)

    with (
        patch("media_bot_v2.engines.direct.requests.head", return_value=head_resp) as mock_head,
        patch("media_bot_v2.engines.direct.requests.get") as mock_get,
        pytest.raises(DownloadTooLargeError) as exc_info,
    ):
        await engine.download(zim_url, dest_dir=tmp_path)

    # HEAD was called
    mock_head.assert_called_once()
    # GET was NEVER called - rejected before streaming
    mock_get.assert_not_called()
    # No files left on disk
    assert not any(tmp_path.iterdir())

    # Message is in Hebrew and contains detected size, max size, and Telegram explanation
    err_text = str(exc_info.value)
    assert "100.0GB" in err_text
    assert "4.0GB" in err_text
    assert "טלגרם אינה תומכת בהעברת קבצים" in err_text


# ==============================================================================
# Lesson 3: Failed provider tried at most once per request
# ==============================================================================


async def test_lesson3_failed_provider_tried_only_once_per_request(tmp_path):
    """When a provider fails, it is never retried again in the same request."""
    _, _, health_tracker = _setup_test_db()

    failing_provider = _MockProvider("tikwm", fail=True, error_msg="timeout")

    registry = ProviderRegistry(tiktok_order=["tikwm", "tikwm"])
    registry.register(failing_provider)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )

    with (
        patch.object(engine, "_download_local_sync") as mock_local,
        pytest.raises(TikTokDownloadError),
    ):
        mock_local.side_effect = RuntimeError("yt-dlp also failed")
        await engine.download("https://www.tiktok.com/@user/video/123", dest_dir=tmp_path)

    # TikWM was called exactly once, NOT twice despite duplicate registration/order
    assert len(failing_provider.fetch_calls) == 1


# ==============================================================================
# Lesson 4: Failure summary includes routes and reasons
# ==============================================================================


async def test_lesson4_failure_message_includes_routes_and_reasons(tmp_path):
    """When all routes fail, the final error message presents a concise summary of tried routes and reasons."""
    _, _, health_tracker = _setup_test_db()

    p1 = _MockProvider("tikwm", fail=True, error_msg="404 not found empty media")
    p2 = _MockProvider("musicaldown", fail=True, error_msg="HTTP Error 500: Server Error")

    registry = ProviderRegistry(tiktok_order=["tikwm", "musicaldown"])
    registry.register(p1)
    registry.register(p2)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )

    with (
        patch.object(engine, "_download_local_sync") as mock_local,
        pytest.raises(TikTokDownloadError) as exc_info,
    ):
        mock_local.side_effect = RuntimeError("Sign in to confirm you're not a bot")
        await engine.download("https://www.tiktok.com/@user/video/123", dest_dir=tmp_path)

    err_text = str(exc_info.value)
    # Checks that attempted routes and their reasons are explicitly present
    assert "פירוט הניסיונות:" in err_text
    assert "tikwm: לא נמצאה כתובת להורדה" in err_text
    assert "musicaldown: שגיאת שרת של הספק" in err_text
    assert "מנוע מקומי (yt-dlp): סרטון דורש התחברות או אימות" in err_text


async def test_lesson4_youtube_failure_message_includes_routes_and_reasons(tmp_path):
    """When YouTube local engine and fallback providers fail, summary contains all attempted routes and reasons."""
    _, _, health_tracker = _setup_test_db()

    ytmp3 = _MockProvider("ytmp3", fail=True, error_msg="HTTP Error 500: Server Error")
    registry = ProviderRegistry(youtube_order=["ytmp3"])
    registry.register(ytmp3)

    engine = YouTubeEngine(
        quality="720",
        max_download_size=100 * 1024 * 1024,
        registry=registry,
        health_tracker=health_tracker,
    )

    with (
        patch.object(
            engine,
            "_download_sync",
            side_effect=YouTubeDownloadError("Sign in to confirm you're not a bot"),
        ),
        pytest.raises(YouTubeDownloadError) as exc_info,
    ):
        await engine.download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", dest_dir=tmp_path)

    err_text = str(exc_info.value)
    assert "פירוט הניסיונות:" in err_text
    assert "מנוע מקומי (yt-dlp): סרטון דורש התחברות או אימות" in err_text
    assert "ytmp3: שגיאת שרת של הספק" in err_text


# ==============================================================================
# Lesson 5: Accurate diagnostics instead of misleading "outdated version"
# ==============================================================================


def test_lesson5_accurate_error_classification():
    """Classification produces exact Hebrew messages for JS runtime, unsupported URL, and format errors."""
    # 1. Missing JS runtime
    js_msg = classify_youtube_error("ERROR: No supported JavaScript runtime could be found")
    assert "JavaScript" in js_msg
    assert "Node.js" in js_msg

    # 2. Unsupported URL
    unsupported_msg = classify_youtube_error("ERROR: Unsupported URL: https://example.com/not-supported")
    assert texts.UNSUPPORTED_URL == unsupported_msg

    # 3. Oversized file
    oversized_msg = texts.format_download_too_large(107374182400, 4294967296)
    assert "100.0GB" in oversized_msg
    assert "4.0GB" in oversized_msg
    assert "טלגרם" in oversized_msg

    # Never blames outdated version
    assert "גרסה מיושנת" not in js_msg
    assert "גרסה מיושנת" not in unsupported_msg


# ==============================================================================
# Lesson 6: Irrelevant routes skipped and logged, never reported as failed
# ==============================================================================


async def test_lesson6_incompatible_provider_skipped_without_recording_failure(tmp_path, caplog):
    """A provider that does not support the URL is skipped, logged with reason, and not recorded as failed."""
    _, _, health_tracker = _setup_test_db()

    # Provider that explicitly does not match this URL
    incompatible_prov = _MockProvider("some_prov", matches_return=False)
    working_prov = _MockProvider("good_prov", matches_return=True)

    registry = ProviderRegistry(tiktok_order=["some_prov", "good_prov"])
    registry.register(incompatible_prov)
    registry.register(working_prov)

    engine = TikTokEngine(
        registry=registry,
        health_tracker=health_tracker,
        max_download_size=100 * 1024 * 1024,
    )

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Length": "10"}
    mock_resp.iter_content.return_value = [b"1234567890"]
    mock_resp.raise_for_status.return_value = None
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with (
        caplog.at_level(logging.INFO),
        patch("media_bot_v2.providers.downloader.requests.get", return_value=mock_resp),
    ):
        result = await engine.download("https://www.tiktok.com/@user/video/123", dest_dir=tmp_path)

    # Incompatible provider was never called
    assert len(incompatible_prov.fetch_calls) == 0
    # Working provider succeeded
    assert len(working_prov.fetch_calls) == 1
    assert result.title == "Title by good_prov"

    # Log recorded the skip reason
    assert any("Skipping TikTok provider some_prov" in r.message for r in caplog.records)


# ==============================================================================
# Lesson 7: Explicit playlist item count on trimming
# ==============================================================================


async def test_lesson7_playlist_trimming_reports_downloaded_out_of_total(tmp_path):
    """When a playlist is trimmed due to credits limit, the message states how many downloaded out of how many."""
    _, credits_service, _ = _setup_test_db()

    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    uploader = _MockUploader()
    progress = _MockProgress()

    # Engine returning a trimmed playlist: 3 downloaded out of 10
    class _TrimmedPlaylistEngine(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            files = []
            for i in range(3):
                p = dest_dir / f"vid_{i}.mp4"
                p.write_bytes(b"content")
                files.append(str(p))
            return DownloadResult(
                file_paths=files,
                title="Playlist",
                playlist_total=10,
                playlist_downloaded=3,
            )

    await pipeline.run(
        user_id=1,
        url="https://www.youtube.com/playlist?list=PL123",
        engine=_TrimmedPlaylistEngine(),
        uploader=uploader,
        progress=progress,
    )

    final_msg = progress.updates[-1]
    assert "3" in final_msg
    assert "10" in final_msg
    assert "הורדו 3 מתוך 10 פריטים" in final_msg


# ==============================================================================
# Lesson 8: Request time budget stops, reports, and cleans up files
# ==============================================================================


async def test_lesson8_request_timeout_stops_reports_and_cleans_up(tmp_path):
    """Exceeding request timeout budget cancels execution, updates progress, and deletes local files."""
    _, credits_service, _ = _setup_test_db()

    # Set a tiny timeout budget of 0.05 seconds
    pipeline = DownloadPipeline(
        credits_service=credits_service,
        download_dir=tmp_path,
        request_timeout=0.05,
    )
    uploader = _MockUploader()
    progress = _MockProgress()

    class _HangingEngine(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Write a partial file before sleeping
            partial = dest_dir / "partial.bin"
            partial.write_bytes(b"partial-data")
            await asyncio.sleep(1.0)  # Hangs beyond 0.05s budget
            return DownloadResult(file_paths=[str(partial)])

    with pytest.raises(TimeoutError):
        await pipeline.run(
            user_id=1,
            url="http://example.com/hanging",
            engine=_HangingEngine(),
            uploader=uploader,
            progress=progress,
        )

    # Progress message updated with timeout message
    assert any(texts.REQUEST_TIMEOUT_EXCEEDED in update for update in progress.updates)
    # All task files are deleted
    assert not any(tmp_path.rglob("*.bin"))
    # No credits were charged
    assert credits_service.get_total_credits(1) == 5
