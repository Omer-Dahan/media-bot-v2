"""Tests for InstagramEngine: URL matching, ID extraction, error classification,
retry policy, progress reporting, size cap enforcement, and download flow.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import yt_dlp

from media_bot_v2.engines.base import DownloadTooLargeError, UnsupportedUrlError
from media_bot_v2.engines.instagram import (
    InstagramDownloadError,
    InstagramEngine,
    _DownloadTooLargeSignal,
    classify_instagram_error,
    extract_instagram_id,
    format_progress_text,
    is_retryable_error,
    matches_instagram_url,
)
from media_bot_v2.telegram import texts

# --- 1. URL Matching Tests ---


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/DFxyz123/",
        "https://instagram.com/p/DFxyz123",
        "http://m.instagram.com/p/DFxyz123/",
        "https://www.instagram.com/reel/C-xyz789/",
        "https://instagram.com/reel/C-xyz789",
        "https://www.instagram.com/reels/C-xyz789/",
        "https://m.instagram.com/reels/C-xyz789/",
        "https://www.instagram.com/tv/B_xyz456/",
        "https://instagram.com/tv/B_xyz456",
        "https://instagr.am/p/DFxyz123/",
        "https://www.instagr.am/reel/C-xyz789/",
        "https://instagr.am/reels/C-xyz789/",
        "https://www.instagram.com/share/p/DFxyz123/",
        "https://www.instagram.com/share/reel/C-xyz789/",
        "https://www.instagram.com/share/SH123456/",
        "https://www.instagram.com/stories/highlights/123456789/",
        "https://www.instagram.com/reel/C-xyz789/?igsh=MWxyz123",
        "https://instagram.com/p/DFxyz123/?utm_source=ig_web_copy_link",
    ],
)
def test_matches_instagram_url_valid(url: str):
    assert matches_instagram_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.tiktok.com/@user/video/7123456789",
        "https://notinstagram.com/p/DFxyz123/",
        "https://fakeinstagram.com/reel/C-xyz789/",
        "https://instagram.com/about/",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/developer/",
        "https://instagram.com/",
        "https://instagr.am/",
        "not-a-valid-url",
        "",
    ],
)
def test_matches_instagram_url_invalid(url: str):
    assert matches_instagram_url(url) is False


# --- 2. Shortcode / ID Extraction Tests ---


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.instagram.com/p/DFxyz123/", "DFxyz123"),
        ("https://instagram.com/p/DFxyz123?utm_source=copy", "DFxyz123"),
        ("https://www.instagram.com/reel/C-xyz789/", "C-xyz789"),
        ("https://www.instagram.com/reels/C-xyz789/", "C-xyz789"),
        ("https://m.instagram.com/reels/C-xyz789/", "C-xyz789"),
        ("https://instagr.am/p/ABC789/", "ABC789"),
        ("https://www.instagr.am/reel/REEL123/", "REEL123"),
        ("https://www.instagram.com/tv/TV_456/", "TV_456"),
        ("https://www.instagram.com/share/p/SHARE_P/", "SHARE_P"),
        ("https://www.instagram.com/share/reel/SHARE_R/", "SHARE_R"),
        ("https://www.instagram.com/stories/highlights/HL999/", "HL999"),
    ],
)
def test_extract_instagram_id_valid(url: str, expected_id: str):
    assert extract_instagram_id(url) == expected_id


def test_extract_instagram_id_unsupported():
    assert extract_instagram_id("https://instagram.com/about/") is None
    assert extract_instagram_id("https://notinstagram.com/p/123/") is None


# --- 3. Error Classification Tests ---


@pytest.mark.parametrize(
    "msg",
    [
        "Instagram API is not granting access",
        "empty media response",
        "login required to view this post",
        "This account is private",
        "This content is private",
        "private account",
        "requires authentication",
        "checkpoint_required",
        "Sign in to confirm you're not a bot",
        "bot detection triggered",
        "User has restricted access",
        "HTTP Error 401: Unauthorized",
        "HTTP Error 403: Forbidden",
    ],
)
def test_classify_instagram_error_private_or_login(msg: str):
    classified = classify_instagram_error(msg)
    assert classified == texts.INSTAGRAM_PRIVATE_OR_LOGIN


@pytest.mark.parametrize(
    "msg",
    [
        "This post has been deleted",
        "Post is unavailable",
        "HTTP Error 404: Not Found",
        "does not exist",
        "has been removed",
    ],
)
def test_classify_instagram_error_not_found(msg: str):
    classified = classify_instagram_error(msg)
    assert classified == texts.INSTAGRAM_NOT_FOUND


@pytest.mark.parametrize(
    "msg",
    [
        "Unsupported URL: http://bad.url",
        "is not a valid URL",
        "unknown url type",
        "url is not supported",
    ],
)
def test_classify_instagram_error_unsupported_url(msg: str):
    classified = classify_instagram_error(msg)
    assert classified == texts.UNSUPPORTED_URL


@pytest.mark.parametrize(
    "msg",
    [
        "unsupported media type for this post",
        "no video formats found",
        "format not available",
        "requested format is not available",
        "live event is currently broadcasting",
        "live stream cannot be downloaded",
    ],
)
def test_classify_instagram_error_unsupported_media(msg: str):
    classified = classify_instagram_error(msg)
    assert classified == texts.INSTAGRAM_UNSUPPORTED_MEDIA


@pytest.mark.parametrize(
    "msg",
    [
        "Connection reset by peer",
        "Connection refused",
        "Connection timed out",
        "timed out",
        "urlopen error [Errno 110] Connection timed out",
        "network is unreachable",
        "no route to host",
        "temporary failure in name resolution",
        "read timed out",
        "RemoteDisconnected: Server disconnected",
    ],
)
def test_classify_instagram_error_network(msg: str):
    classified = classify_instagram_error(msg)
    assert classified == texts.INSTAGRAM_NETWORK_ERROR


def test_classify_instagram_error_empty_and_unknown():
    assert "לא התקבל קובץ מדיה" in classify_instagram_error(None)
    assert "לא התקבל קובץ מדיה" in classify_instagram_error("")

    unknown_msg = "some totally novel error happened with post 123"
    classified = classify_instagram_error(unknown_msg)
    # Whitelist: an unrecognised error never echoes its raw text to the user.
    assert classified == texts.INSTAGRAM_GENERIC_FAILURE
    assert unknown_msg not in classified
    # Ensure no misleading suggestions like "update yt-dlp"
    assert "yt-dlp" not in classified


# --- 4. Retry Policy Tests ---


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("connection reset by peer", True),
        ("timed out", True),
        ("network is unreachable", True),
        ("HTTP Error 500: Internal Server Error", True),
        ("HTTP Error 503: Service Unavailable", True),
        ("login required", False),
        ("Instagram API is not granting access", False),
        ("This account is private", False),
        ("This post has been deleted", False),
        ("unsupported media", False),
        ("", False),
        (None, False),
    ],
)
def test_is_retryable_error(msg: str | None, expected: bool):
    assert is_retryable_error(msg) == expected


# --- 5. Progress Formatting Tests ---


def test_format_progress_text_downloading():
    d = {
        "status": "downloading",
        "downloaded_bytes": 10 * 1024 * 1024,
        "total_bytes": 20 * 1024 * 1024,
        "speed": 2 * 1024 * 1024,
        "eta": 5,
    }
    text = format_progress_text(d)
    assert text is not None
    assert texts.DOWNLOADING in text
    assert "50%" in text
    assert "10.0MB/20.0MB" in text
    assert "2.0MB/s" in text
    assert "5 שניות" in text


def test_format_progress_text_finished():
    d = {"status": "finished"}
    text = format_progress_text(d)
    assert text == texts.PROCESSING


# --- 6. Engine Configuration & Options ---


def test_instagram_engine_ydl_opts_defaults(tmp_path: Path):
    loop = asyncio.new_event_loop()
    try:
        engine = InstagramEngine(max_download_size=100 * 1024 * 1024)
        opts = engine._build_ydl_opts(tmp_path, loop)

        # The cap is enforced by our own progress hook, not yt-dlp's max_filesize
        # (which skips silently with a Content-Length and is ignored without one).
        assert "max_filesize" not in opts
        assert opts["noplaylist"] is True
        assert opts["quiet"] is True
        assert "impersonate" in opts
        assert "cookiefile" not in opts
    finally:
        loop.close()


def test_instagram_engine_ydl_opts_with_cookies(tmp_path: Path):
    cookie_file = tmp_path / "ig_cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")

    loop = asyncio.new_event_loop()
    try:
        engine = InstagramEngine(
            max_download_size=50 * 1024 * 1024,
            cookies_file=str(cookie_file),
            force_ipv4=True,
        )
        opts = engine._build_ydl_opts(tmp_path, loop)

        assert opts["cookiefile"] == str(cookie_file)
        assert opts["source_address"] == "0.0.0.0"
    finally:
        loop.close()


# --- 7. Size Cap Enforcement Tests ---


def test_progress_hook_raises_download_too_large_signal():
    loop = asyncio.new_event_loop()
    try:
        engine = InstagramEngine(max_download_size=10 * 1024 * 1024)
        hook = engine._make_progress_hook(loop)

        hook({"status": "downloading", "total_bytes": 5 * 1024 * 1024})

        with pytest.raises(_DownloadTooLargeSignal):
            hook({"status": "downloading", "total_bytes": 15 * 1024 * 1024})
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_download_too_large_error_surfaces(tmp_path: Path):
    engine = InstagramEngine(max_download_size=10 * 1024 * 1024)

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            hook = self.opts["progress_hooks"][0]
            hook({"status": "downloading", "total_bytes": 20 * 1024 * 1024})
            return {}

    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(DownloadTooLargeError):
        await engine.download("https://www.instagram.com/reel/C-xyz789/", dest_dir=tmp_path)


# --- 8. Download Flow & Error Handling Tests ---


@pytest.mark.asyncio
async def test_download_success(tmp_path: Path):
    engine = InstagramEngine(max_download_size=100 * 1024 * 1024)
    media_file = tmp_path / "post [DFxyz123].mp4"
    media_file.write_bytes(b"dummy video data")

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            return {
                "id": "DFxyz123",
                "title": "A great Instagram Reel",
                "requested_downloads": [{"filepath": str(media_file)}],
            }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        result = await engine.download("https://www.instagram.com/reel/DFxyz123/", dest_dir=tmp_path)

    assert result.file_paths == [str(media_file)]
    assert result.title == "A great Instagram Reel"


@pytest.mark.asyncio
async def test_download_private_content_raises_hebrew_error(tmp_path: Path):
    engine = InstagramEngine(max_download_size=100 * 1024 * 1024)

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("Instagram API is not granting access to this content")

    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(InstagramDownloadError) as exc_info:
        await engine.download("https://www.instagram.com/reel/PRIVATE123/", dest_dir=tmp_path)

    assert texts.INSTAGRAM_PRIVATE_OR_LOGIN in str(exc_info.value)


@pytest.mark.asyncio
async def test_download_unsupported_url_raises_immediately(tmp_path: Path):
    engine = InstagramEngine(max_download_size=100 * 1024 * 1024)
    with pytest.raises(UnsupportedUrlError):
        await engine.download("https://notinstagram.com/p/123/", dest_dir=tmp_path)
