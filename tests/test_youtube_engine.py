"""YouTubeEngine: format selection, error classification, retry, size cap,
and progress reporting. Never touches the network or a real yt-dlp
extraction - `yt_dlp.YoutubeDL` is monkeypatched with a fake that returns or
raises exactly what each test needs, per this stage's "no downloads from
the internet" constraint (live diagnosis is --simulate/--list-formats only,
and even that is not run from automated tests)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
import yt_dlp

from media_bot_v2.engines.base import DownloadTooLargeError
from media_bot_v2.engines.youtube import (
    YouTubeDownloadError,
    YouTubeEngine,
    build_format_selector,
    classify_youtube_error,
    extract_video_id,
    format_progress_text,
    is_playlist_url,
    is_retryable_error,
)

# --- pure helpers -----------------------------------------------------


@pytest.mark.parametrize(
    "quality,expected_height",
    [("1080", 1080), ("720", 720), ("480", 480), ("360", 360)],
)
def test_build_format_selector_prefers_progressive_at_requested_height(quality, expected_height):
    selector = build_format_selector(quality)
    progressive, merge_fallback, best_fallback = selector.split("/")
    assert progressive == f"best[vcodec!=none][acodec!=none][height<={expected_height}]"
    assert merge_fallback == f"bestvideo[height<={expected_height}]+bestaudio"
    assert best_fallback == "best"


def test_build_format_selector_audio_only():
    assert build_format_selector("audio") == "bestaudio/best"


def test_build_format_selector_rejects_unknown_quality():
    with pytest.raises(ValueError):
        build_format_selector("4k")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/playlist?list=PL123", None),
        ("https://example.com/not-youtube", None),
    ],
)
def test_extract_video_id(url, expected):
    assert extract_video_id(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/playlist?list=PL123", True),
        ("https://www.youtube.com/watch?v=abc&list=PL123", False),  # single video, incidentally in a playlist
        ("https://www.youtube.com/watch?v=abc", False),
        ("https://youtu.be/abc", False),
    ],
)
def test_is_playlist_url(url, expected):
    assert is_playlist_url(url) is expected


def test_classify_youtube_error_js_runtime_missing():
    msg = classify_youtube_error("ERROR: The page needs to be reloaded (missing JS runtime)")
    assert "JavaScript" in msg or "runtime" in msg.lower()


def test_classify_youtube_error_po_token():
    msg = classify_youtube_error("Some formats are missing a required PO Token")
    assert "PO token" in msg or "POTOKEN" in msg


def test_classify_youtube_error_bot_detection():
    msg = classify_youtube_error("Sign in to confirm you're not a bot")
    assert "בוט" in msg


def test_classify_youtube_error_cookies():
    msg = classify_youtube_error("cookies are no longer valid")
    assert "cookies" in msg


def test_classify_youtube_error_private_video():
    msg = classify_youtube_error("ERROR: [youtube] abc: Private video")
    assert "פרטי" in msg or "נמחק" in msg


def test_classify_youtube_error_geo_restricted():
    msg = classify_youtube_error("The uploader has not made this video available in your country")
    assert "גיאוגרפית" in msg


def test_classify_youtube_error_live_stream():
    msg = classify_youtube_error("This live event will begin in 2 hours, premieres in a bit")
    assert "שידור חי" in msg


def test_classify_youtube_error_playlist_unavailable():
    msg = classify_youtube_error("The playlist does not exist")
    assert "פלייליסט" in msg


def test_classify_youtube_error_unknown_falls_back_to_truncated_message():
    msg = classify_youtube_error("some totally novel yt-dlp error the classifier has never seen")
    assert "some totally novel" in msg


def test_classify_youtube_error_no_message():
    assert classify_youtube_error(None) != ""


def test_is_retryable_error_network_patterns():
    assert is_retryable_error("Connection reset by peer")
    assert is_retryable_error("Read timed out")
    assert is_retryable_error("HTTP Error 503: Service Unavailable")


def test_is_retryable_error_permanent_failures_are_not_retried():
    assert not is_retryable_error("This video is private")
    assert not is_retryable_error("Sign in to confirm you're not a bot")
    assert not is_retryable_error(None)


def test_format_progress_text_downloading_includes_percent_speed_eta():
    text = format_progress_text(
        {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 1024, "eta": 30}
    )
    assert "50%" in text
    assert "מהירות" in text
    assert "זמן משוער" in text


def test_format_progress_text_finished_status():
    assert format_progress_text({"status": "finished"}) is not None


def test_format_progress_text_ignores_unknown_status():
    assert format_progress_text({"status": "some-future-status"}) is None


# --- YouTubeEngine.download (yt_dlp.YoutubeDL fully mocked) -----------


class _FakeYoutubeDL:
    """Stand-in for yt_dlp.YoutubeDL. Tests configure `_next_result` (a dict
    to return from extract_info) or `_next_error` (an exception to raise);
    `calls` records every ydl_opts dict the engine constructed, so retry
    tests can assert call count without any real extraction happening."""

    calls: ClassVar[list[dict]] = []
    results: ClassVar[list[object]] = []  # each entry is either a dict or an Exception instance

    def __init__(self, opts):
        self.opts = opts
        _FakeYoutubeDL.calls.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        outcome = _FakeYoutubeDL.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _reset_fake_ytdlp(monkeypatch):
    _FakeYoutubeDL.calls = []
    _FakeYoutubeDL.results = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    yield


def _single_video_info(path: str, title: str = "My Video") -> dict:
    return {"title": title, "requested_downloads": [{"filepath": path}]}


async def test_download_returns_result_from_single_video_info(tmp_path):
    dest = tmp_path / "task"
    _FakeYoutubeDL.results = [_single_video_info(str(dest / "video.mp4"))]

    engine = YouTubeEngine(quality="720", max_download_size=4 * 1024 * 1024 * 1024)
    result = await engine.download("https://youtu.be/abc12345678", dest_dir=dest)

    assert result.file_paths == [str(dest / "video.mp4")]
    assert result.title == "My Video"
    assert len(_FakeYoutubeDL.calls) == 1
    assert _FakeYoutubeDL.calls[0]["format"] == build_format_selector("720")


async def test_download_passes_force_ipv4_as_source_address(tmp_path):
    _FakeYoutubeDL.results = [_single_video_info(str(tmp_path / "v.mp4"))]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000, force_ipv4=True)

    await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)

    assert _FakeYoutubeDL.calls[0]["source_address"] == "0.0.0.0"


async def test_download_omits_source_address_by_default(tmp_path):
    _FakeYoutubeDL.results = [_single_video_info(str(tmp_path / "v.mp4"))]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000)

    await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)

    assert "source_address" not in _FakeYoutubeDL.calls[0]


async def test_download_sets_playlist_end_only_for_playlist_requests(tmp_path):
    _FakeYoutubeDL.results = [_single_video_info(str(tmp_path / "v.mp4"))]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000, playlist_item_limit=5)

    await engine.download("https://youtube.com/playlist?list=PL1", dest_dir=tmp_path)

    assert _FakeYoutubeDL.calls[0]["noplaylist"] is False
    assert _FakeYoutubeDL.calls[0]["playlistend"] == 5


async def test_download_single_video_forces_noplaylist(tmp_path):
    _FakeYoutubeDL.results = [_single_video_info(str(tmp_path / "v.mp4"))]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000)

    await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)

    assert _FakeYoutubeDL.calls[0]["noplaylist"] is True
    assert "playlistend" not in _FakeYoutubeDL.calls[0]


async def test_download_retries_network_error_and_then_succeeds(tmp_path):
    _FakeYoutubeDL.results = [
        yt_dlp.utils.DownloadError("Connection reset by peer"),
        _single_video_info(str(tmp_path / "v.mp4")),
    ]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000, max_retries=2)

    result = await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)

    assert len(_FakeYoutubeDL.calls) == 2  # one retry, then success
    assert result.file_paths == [str(tmp_path / "v.mp4")]


async def test_download_gives_up_after_max_retries_with_classified_message(tmp_path):
    _FakeYoutubeDL.results = [
        yt_dlp.utils.DownloadError("Connection timed out"),
        yt_dlp.utils.DownloadError("Connection timed out"),
        yt_dlp.utils.DownloadError("Connection timed out"),
    ]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000, max_retries=2)

    with pytest.raises(YouTubeDownloadError) as excinfo:
        await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)

    assert len(_FakeYoutubeDL.calls) == 3  # initial attempt + 2 retries
    assert "רשת" in str(excinfo.value)


async def test_download_does_not_retry_permanent_failure():
    """A private-video error will never succeed on retry - retrying it just
    delays the correct failure message and wastes a request to YouTube."""
    _FakeYoutubeDL.results = [yt_dlp.utils.DownloadError("This video is private")]
    engine = YouTubeEngine(quality="720", max_download_size=1_000_000_000, max_retries=2)

    with pytest.raises(YouTubeDownloadError) as excinfo:
        await engine.download("https://youtu.be/abc12345678", dest_dir=Path("/tmp"))

    assert len(_FakeYoutubeDL.calls) == 1  # no retry attempted
    assert "פרטי" in str(excinfo.value) or "נמחק" in str(excinfo.value)


async def test_download_raises_too_large_when_declared_total_exceeds_cap(tmp_path):
    """The progress hook itself enforces the cap mid-download (max_filesize
    only filters formats at selection time, which can't see a live total
    that turns out larger than estimated)."""

    class _RaisingDL(_FakeYoutubeDL):
        def extract_info(self, url, download=True):
            from media_bot_v2.engines.youtube import _DownloadTooLargeSignal

            raise _DownloadTooLargeSignal("9999999999 bytes exceeds the 1000000 byte limit")

    original = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = _RaisingDL
    try:
        engine = YouTubeEngine(quality="720", max_download_size=1_000_000)
        with pytest.raises(DownloadTooLargeError):
            await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)
    finally:
        yt_dlp.YoutubeDL = original


async def test_download_reports_progress_through_the_reporter(tmp_path):
    updates: list[str] = []

    class _Progress:
        async def update(self, text: str) -> None:
            updates.append(text)

    class _HookFiringDL(_FakeYoutubeDL):
        def extract_info(self, url, download=True):
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            outcome = _FakeYoutubeDL.results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    import asyncio

    _FakeYoutubeDL.results = [_single_video_info(str(tmp_path / "v.mp4"))]

    original = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = _HookFiringDL
    try:
        engine = YouTubeEngine(
            quality="720", max_download_size=1_000_000_000, progress=_Progress()
        )
        await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)
        await asyncio.sleep(0.05)  # let run_coroutine_threadsafe's future actually run
    finally:
        yt_dlp.YoutubeDL = original

    assert any("50%" in u for u in updates)
