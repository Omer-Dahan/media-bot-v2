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
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User
from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.instagram import InstagramDownloadError, InstagramEngine
from media_bot_v2.engines.tiktok import (
    TikTokDownloadError,
    TikTokEngine,
    matches_tiktok_url,
)
from media_bot_v2.engines.youtube import (
    YouTubeDownloadError,
    YouTubeEngine,
    _DownloadCancelledSignal,
    _result_from_info,
    classify_youtube_error,
    matches_youtube_url,
    summarize_provider_failure,
    summarize_ytdlp_failure,
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
from media_bot_v2.telegram.router import (
    TIKTOK_HOSTS,
    YOUTUBE_HOSTS,
    _host_matches,
)


class _MockProgress:
    def __init__(self) -> None:
        self.updates: list[str] = []

    async def update(self, text: str) -> None:
        self.updates.append(text)


class _MockUploader:
    def __init__(self) -> None:
        self.sent: list[Path] = []
        self.archived: list[object] = []

    async def send_file(self, path: Path, *, caption: str | None = None, **kwargs) -> MagicMock:
        self.sent.append(path)
        return MagicMock(id=len(self.sent))

    async def copy_to_archive(self, message: object, **kwargs) -> MagicMock:
        self.archived.append(message)
        return MagicMock(id=len(self.archived) + 100)

    async def send_cached(self, archive_chat: str, message_ids: list[int], **kwargs) -> MagicMock:
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


class _SlowStreamingSource:
    """Simulates a slow server transferring chunks over real time."""

    def __init__(self, total_bytes: int = 20 * 1024 * 1024, chunk_size: int = 64 * 1024) -> None:
        self.total_bytes = total_bytes
        self.chunk_size = chunk_size
        self.bytes_sent = 0
        self.closed = False
        self.exited = False
        self._lock = threading.Lock()

    def iter_content(self, chunk_size: int | None = None):
        chunk_data = b"x" * self.chunk_size
        while self.bytes_sent < self.total_bytes:
            with self._lock:
                if self.closed:
                    break
            time.sleep(0.01)  # Real time delay in streaming worker thread
            with self._lock:
                if self.closed:
                    break
                self.bytes_sent += len(chunk_data)
            yield chunk_data

    def close(self) -> None:
        with self._lock:
            self.closed = True

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Length": str(self.total_bytes),
            "Content-Type": "application/octet-stream",
        }

    def raise_for_status(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        # Unlike requests.Response, leaving the `with` block does NOT mark the
        # source closed: `closed` only turns True if the cancellation path
        # itself closed the connection.
        self.exited = True


async def test_lesson8_active_transfer_actually_stops_on_timeout_with_byte_counter(tmp_path):
    """Exceeding download timeout actually halts background thread streaming,
    stops byte transfer well before total payload, and cleans up partial files."""
    _, credits_service, _ = _setup_test_db()

    pipeline = DownloadPipeline(
        credits_service=credits_service,
        download_dir=tmp_path,
        download_timeout=0.08,
    )
    uploader = _MockUploader()
    progress = _MockProgress()
    direct_engine = DirectEngine()

    source = _SlowStreamingSource(total_bytes=20 * 1024 * 1024)

    with (
        # No real network: the preflight HEAD is stubbed to fail (the engine
        # then proceeds to the GET, as it does for servers that reject HEAD).
        patch(
            "media_bot_v2.engines.direct.requests.head",
            side_effect=ConnectionError("offline"),
        ) as mock_head,
        patch("media_bot_v2.engines.direct.requests.get", return_value=source),
        pytest.raises(TimeoutError),
    ):
        await pipeline.run(
            user_id=1,
            url="https://example.com/big_file.bin",
            engine=direct_engine,
            uploader=uploader,
            progress=progress,
        )

    mock_head.assert_called_once()

    # Transfer was stopped far before transferring all 20MB, and it was the
    # cancellation path (not the context manager exiting) that closed the source.
    bytes_at_stop = source.bytes_sent
    assert 0 < bytes_at_stop < 20 * 1024 * 1024
    assert source.closed is True

    # Confirm thread has actually stopped transferring: wait and verify byte counter is frozen
    await asyncio.sleep(0.05)
    assert source.bytes_sent == bytes_at_stop

    # Progress message updated with timeout message
    assert any(texts.REQUEST_TIMEOUT_EXCEEDED in update for update in progress.updates)
    # All task files are cleaned up from disk
    assert not any(tmp_path.rglob("*.bin"))
    # No credits were charged
    assert credits_service.get_total_credits(1) == 5


def test_lesson8_ytdlp_progress_hook_raises_cancelled_signal_on_cancel_token():
    token = CancellationToken()
    engine = YouTubeEngine(quality="720", max_download_size=1000)
    loop = asyncio.new_event_loop()
    hook = engine._make_progress_hook(loop, cancel_token=token)

    # When not cancelled, hook runs without raising
    hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 500})

    # Set cancellation
    token.set()
    with pytest.raises(_DownloadCancelledSignal):
        hook({"status": "downloading", "downloaded_bytes": 200, "total_bytes": 500})
    loop.close()


# ==============================================================================
# M4.1 Regression Tests for Independent Verification Findings
# ==============================================================================


def test_m4_1_finding2_summarize_ytdlp_failure_preserves_english_and_handles_hebrew():
    """Finding 2: failure summary works with English source message and with translated Hebrew message."""
    # 1. Error wrapped in YouTubeDownloadError with original English error
    err1 = YouTubeDownloadError(
        "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (Node.js",
        original_error="ERROR: No supported JavaScript runtime could be found",
    )
    assert summarize_ytdlp_failure(err1) == "חסר runtime של JavaScript"

    # 2. String that was already translated to Hebrew
    err2 = "שגיאת פענוח ביוטיוב: חסר בשרת runtime של JavaScript (Node.js"
    assert summarize_ytdlp_failure(err2) == "חסר runtime של JavaScript"

    # 3. Bot detection / auth required in Hebrew
    err3 = "סרטון זה דורש אימות או זיהוי בוט"
    assert summarize_ytdlp_failure(err3) == "סרטון דורש התחברות או אימות"


async def test_m4_1_finding3_youtube_oversized_uses_exact_hebrew_and_real_limit(tmp_path):
    """Finding 3: a download that crosses the cap mid-stream (no declared total)
    surfaces the unified Hebrew message with the detected size and real limit.
    (tests/test_m4_2.py repeats this against real yt-dlp and a local server.)"""

    class _StreamingYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            for downloaded in range(1_000_000, 6_000_000, 1_000_000):
                for hook in self.opts["progress_hooks"]:
                    hook({"status": "downloading", "downloaded_bytes": downloaded})
            return {"id": "x", "requested_downloads": [{"filepath": str(tmp_path / "x.mp4")}]}

    engine = YouTubeEngine(quality="720", max_download_size=2 * 1024 * 1024)
    with patch("yt_dlp.YoutubeDL", _StreamingYDL), pytest.raises(DownloadTooLargeError) as exc_info:
        await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)
    msg = str(exc_info.value)
    assert "הקובץ גדול מדי" in msg
    assert "2.0MB" in msg  # the configured limit
    assert "2.9MB" in msg  # first hook call past the limit: 3,000,000 bytes
    assert "bytes exceeds" not in msg


def test_m4_1_finding4_no_internal_path_or_host_leak_in_error_messages():
    """Finding 4: internal paths (/srv/media/...) and provider hosts never leak to user."""
    # File path leak
    leaky_path_msg = classify_youtube_error("ERROR: /srv/media/tmp/abc123.part: Permission denied")
    assert "/srv/media" not in leaky_path_msg
    assert ".part" not in leaky_path_msg
    assert leaky_path_msg == "ההורדה מיוטיוב נכשלה. נסה שוב או שלח קישור אחר."

    # Hostname leak
    leaky_host_msg = classify_youtube_error("Failed to connect to internal-supplier.cloud.internal:8080")
    assert "internal-supplier" not in leaky_host_msg
    assert ".internal" not in leaky_host_msg
    assert leaky_host_msg == "ההורדה מיוטיוב נכשלה. נסה שוב או שלח קישור אחר."

    # Failure summaries do not leak raw [:60]
    assert summarize_ytdlp_failure("/srv/media/tmp/abc123.part died") == "שגיאה במנוע המקומי"
    assert summarize_provider_failure("http://supplier.secret.internal:8080/v1 failed") == "שגיאה בספק"


def test_m4_1_finding5_host_only_matching_rejects_query_and_prefix_spoofs():
    """Finding 5: URL routing checks parsed host, not substring on entire URL."""
    spoofed_query = "https://files.example.com/f.zim?ref=youtube.com"
    spoofed_domain = "https://notyoutube.com/watch?v=x"
    spoofed_tiktok = "https://evil-tiktok.com/@user/video/123"

    assert DirectEngine().matches(spoofed_query) is True
    assert matches_youtube_url(spoofed_query) is False
    assert matches_youtube_url(spoofed_domain) is False
    assert matches_tiktok_url(spoofed_tiktok) is False
    assert TikWMProvider().matches(spoofed_tiktok) is False

    # router _host_matches rejects query param spoof and prefix spoof
    assert _host_matches(spoofed_query, YOUTUBE_HOSTS) is False
    assert _host_matches(spoofed_domain, YOUTUBE_HOSTS) is False
    assert _host_matches(spoofed_tiktok, TIKTOK_HOSTS) is False

    # valid youtube hosts match
    assert matches_youtube_url("https://www.youtube.com/watch?v=123") is True
    assert matches_youtube_url("https://youtu.be/123") is True
    assert matches_youtube_url("https://m.youtube.com/watch?v=123") is True


async def test_m4_1_finding6_local_route_is_attempted_once(tmp_path):
    """Finding 6: a transient failure on the local route is attempted exactly once
    by default, for both yt-dlp engines (checked by counting real calls)."""
    class _FailingYDL:
        calls = 0

        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            type(self).calls += 1
            raise yt_dlp.utils.DownloadError("Connection reset by peer")

    with patch("yt_dlp.YoutubeDL", _FailingYDL), pytest.raises(YouTubeDownloadError):
        await YouTubeEngine(quality="720", max_download_size=1000).download(
            "https://youtu.be/abc12345678", dest_dir=tmp_path
        )
    assert _FailingYDL.calls == 1

    with patch("yt_dlp.YoutubeDL", _FailingYDL), pytest.raises(InstagramDownloadError):
        await InstagramEngine(max_download_size=1000).download(
            "https://www.instagram.com/reel/abc/", dest_dir=tmp_path
        )
    assert _FailingYDL.calls == 2  # exactly one more call, not two


async def test_m4_1_finding7_multipart_upload_failure_charges_delivered_part_only(tmp_path):
    """Finding 7: multi-part upload failure charges the delivered part and caches nothing."""
    session_factory, credits_service, _ = _setup_test_db()
    cache_store = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(
        credits_service=credits_service,
        download_dir=tmp_path,
        download_timeout=30.0,
        upload_timeout=30.0,
    )

    class _TwoPartEngine(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
            dest_dir.mkdir(parents=True, exist_ok=True)
            p1 = dest_dir / "part1.bin"
            p2 = dest_dir / "part2.bin"
            p1.write_bytes(b"A" * 1000)
            p2.write_bytes(b"B" * 1000)
            return DownloadResult(file_paths=[str(p1), str(p2)], title="TwoPart")

    class _PartialFailUploader(_MockUploader):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def send_file(self, path: Path, *, caption: str | None = None, **kwargs) -> MagicMock:
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("Upload network dropped on part 2")
            return await super().send_file(path, caption=caption)

    uploader = _PartialFailUploader()
    progress = _MockProgress()
    cache_key = compute_cache_key("test_url", "720")

    with pytest.raises(RuntimeError):
        await pipeline.run(
            user_id=1,
            url="http://example.com/twopart",
            engine=_TwoPartEngine(),
            uploader=uploader,
            progress=progress,
            cache=cache_store,
            cache_key=cache_key,
            archive_channel="@archive",
        )

    # User received part 1 only: charged by the delivered volume (1000 bytes -> 1 credit)
    with session_factory() as session:
        user = session.query(User).filter(User.user_id == 1).one()
        assert user.free == 4  # 5 - 1 = 4
        assert user.bandwidth_used == 1000

    # A partial delivery is never cached (M4.2): a repeat request must not be
    # served the delivered subset from cache and told "done".
    assert cache_store.get(cache_key) is None


async def test_m4_1_finding8_foreign_timeout_error_reports_download_failed_not_budget(tmp_path):
    """Finding 8: foreign TimeoutError from socket is reported as download failure, not budget timeout."""
    _, credits_service, _ = _setup_test_db()
    pipeline = DownloadPipeline(
        credits_service=credits_service,
        download_dir=tmp_path,
        download_timeout=60.0,
    )
    uploader = _MockUploader()
    progress = _MockProgress()

    class _ForeignTimeoutEngine(BaseEngine):
        def matches(self, url: str) -> bool:
            return True

        async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
            raise TimeoutError("Socket read timeout from upstream")

    with pytest.raises(TimeoutError):
        await pipeline.run(
            user_id=1,
            url="http://example.com/hang",
            engine=_ForeignTimeoutEngine(),
            uploader=uploader,
            progress=progress,
        )

    # Must report DOWNLOAD_FAILED, not REQUEST_TIMEOUT_EXCEEDED
    assert progress.updates[-1] == texts.DOWNLOAD_FAILED
    assert not any(texts.REQUEST_TIMEOUT_EXCEEDED in u for u in progress.updates)


def test_m4_1_finding9_playlist_trimming_reasons():
    """Finding 9: playlist trimming states the exact reason (credits limit vs unavailable)."""
    # 1. Trimming due to credits limit
    info_limited = {
        "_type": "playlist",
        "entries": [{"filepath": "/tmp/a.mp4"}, {"filepath": "/tmp/b.mp4"}],
        "playlist_count": 10,
        "title": "My Playlist",
    }
    res_limited = _result_from_info(info_limited, playlist_item_limit=2)
    assert res_limited.playlist_downloaded == 2
    assert res_limited.playlist_total == 10
    assert "יתרת הקרדיטים" in str(res_limited.playlist_trimmed_reason)

    # 2. Trimming due to unavailable items (no item limit)
    res_unavailable = _result_from_info(info_limited, playlist_item_limit=None)
    assert "אינם זמינים" in str(res_unavailable.playlist_trimmed_reason)


async def test_m4_1_finding10_direct_engine_rejects_html_content_type(tmp_path):
    """Finding 10: direct download rejects HTML Content-Type with UnsupportedUrlError."""
    engine = DirectEngine()

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8", "Content-Length": "500"}
    mock_resp.raise_for_status.return_value = None
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("media_bot_v2.engines.direct.requests.get", return_value=mock_resp):
        with pytest.raises(UnsupportedUrlError) as exc_info:
            await engine.download("https://example.com/page.html", dest_dir=tmp_path)
        assert "HTML" in str(exc_info.value)
