"""Tests for FacebookEngine: URL matching, ID extraction, format selector,
title cleanup, redirect resolution (SSRF guard), error classification, retry
policy, size cap enforcement, and download flow (single file + playlist).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import yt_dlp

from media_bot_v2.engines import facebook as facebook_engine
from media_bot_v2.engines.base import DownloadTooLargeError, UnsupportedUrlError
from media_bot_v2.engines.facebook import (
    FORMAT_SELECTOR,
    FacebookDownloadError,
    FacebookEngine,
    _DownloadTooLargeSignal,
    classify_facebook_error,
    extract_facebook_id,
    format_progress_text,
    is_retryable_error,
    matches_facebook_url,
    resolve_facebook_url,
)
from media_bot_v2.telegram import texts

# --- 1. URL Matching Tests ---


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/reel/1127463233306375",
        "https://facebook.com/reel/1127463233306375/",
        "https://m.facebook.com/reels/1127463233306375",
        "https://www.facebook.com/watch/?v=1127463233306375",
        "https://www.facebook.com/watch?v=1127463233306375",
        "https://www.facebook.com/someuser/videos/1127463233306375/",
        "https://www.facebook.com/videos/1127463233306375/",
        "https://www.facebook.com/video.php?v=1127463233306375",
        "https://www.facebook.com/story.php?story_fbid=123&id=456",
        "https://www.facebook.com/permalink.php?story_fbid=123&id=456",
        "https://www.facebook.com/groups/123456789/posts/987654321/",
        "https://www.facebook.com/share/r/abc123/",
        "https://www.facebook.com/share/v/abc123/",
        "https://www.facebook.com/share/abc123/",
        "https://fb.watch/abc123/",
        "https://fb.watch/abc123",
        "https://fb.me/abc123",
        "https://web.facebook.com/plugins/video.php?href=https%3A%2F%2Ffacebook.com",
        "https://fb.com/reel/1127463233306375",
    ],
)
def test_matches_facebook_url_valid(url: str):
    assert matches_facebook_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/zuck",
        "https://www.facebook.com/photo.php?fbid=123",
        "https://www.facebook.com/someuser/photos/a.123/456/",
        "https://www.facebook.com/marketplace/item/123/",
        "https://www.facebook.com/groups/123456789/",
        "https://www.facebook.com/",
        "https://www.facebook.com/watch/",
        "https://fb.watch/",
        "https://notfacebook.com/reel/123",
        "https://fakefacebook.com/reel/123",
        "https://youtube.com/watch?v=dQw4w9WgXcQ",
        "not-a-valid-url",
        "",
    ],
)
def test_matches_facebook_url_invalid(url: str):
    assert matches_facebook_url(url) is False


# --- 2. ID Extraction Tests ---


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.facebook.com/reel/1127463233306375", "1127463233306375"),
        ("https://facebook.com/reel/1127463233306375/", "1127463233306375"),
        ("https://m.facebook.com/reels/1127463233306375", "1127463233306375"),
        ("https://www.facebook.com/watch/?v=1127463233306375", "1127463233306375"),
        ("https://www.facebook.com/video.php?v=1127463233306375", "1127463233306375"),
        ("https://www.facebook.com/someuser/videos/1127463233306375/", "1127463233306375"),
        ("https://www.facebook.com/videos/1127463233306375/", "1127463233306375"),
    ],
)
def test_extract_facebook_id_numeric(url: str, expected_id: str):
    assert extract_facebook_id(url) == expected_id


def test_extract_facebook_id_hashes_non_numeric_share_token():
    id1 = extract_facebook_id("https://www.facebook.com/share/r/abc123/")
    id2 = extract_facebook_id("https://www.facebook.com/share/r/abc123/")
    id3 = extract_facebook_id("https://www.facebook.com/share/r/different/")
    assert id1 is not None
    assert id1 == id2
    assert id1 != id3
    assert id1 != "abc123"  # hashed, not the raw token


def test_extract_facebook_id_hashes_fb_watch_token():
    assert extract_facebook_id("https://fb.watch/abc123/") is not None
    assert extract_facebook_id("https://fb.watch/abc123/") == extract_facebook_id("https://fb.watch/abc123")


def test_extract_facebook_id_unsupported():
    assert extract_facebook_id("https://www.facebook.com/zuck") is None
    assert extract_facebook_id("https://notfacebook.com/reel/123") is None


# --- 3. Format Selector ---


def test_format_selector_order():
    parts = FORMAT_SELECTOR.split("/")
    assert parts == [
        "hd[ext=mp4]",
        "sd[ext=mp4]",
        "best[ext=mp4][vcodec^=avc]",
        "bestvideo[ext=mp4][vcodec^=avc]+bestaudio[acodec^=mp4a]",
        "best[ext=mp4]",
        "best",
    ]


def test_engine_uses_format_selector(tmp_path: Path):
    loop = asyncio.new_event_loop()
    try:
        engine = FacebookEngine(max_download_size=100 * 1024 * 1024)
        opts = engine._build_ydl_opts(tmp_path, loop)
        assert opts["format"] == FORMAT_SELECTOR
        assert opts["merge_output_format"] == "mp4"
        assert "noplaylist" not in opts
    finally:
        loop.close()


# --- 4. Title Cleanup ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.6K views · 13K reactions ｜ My cool reel", "My cool reel"),
        ("350 comments · 87 shares ｜ Another title", "Another title"),
        ("1.2M views ｜ Just views", "Just views"),
        ("No counters here", "No counters here"),
        (None, "Facebook"),
        ("", "Facebook"),
    ],
)
def test_title_cleanup(raw: str | None, expected: str):
    assert facebook_engine._clean_title(raw) == expected


def test_title_cleanup_caps_at_100_chars():
    long_title = "x" * 200
    assert len(facebook_engine._clean_title(long_title)) == 100


# --- 5. Error Classification Tests ---


@pytest.mark.parametrize(
    "msg",
    [
        "Login required to view this content",
        "You must log in to continue",
        "This video requires a login",
        "age-restricted video",
        "cookies are required",
        "checkpoint_required",
        "Sign in to confirm you're not a bot",
        "HTTP Error 401: Unauthorized",
        "HTTP Error 403: Forbidden",
    ],
)
def test_classify_facebook_error_private_or_login(msg: str):
    assert classify_facebook_error(msg) == texts.FACEBOOK_PRIVATE_OR_LOGIN


@pytest.mark.parametrize(
    "msg",
    [
        "This video is no longer available",
        "This content isn't available right now",
        "Video unavailable",
        "HTTP Error 404: Not Found",
        "does not exist",
        "has been removed",
    ],
)
def test_classify_facebook_error_not_found(msg: str):
    assert classify_facebook_error(msg) == texts.FACEBOOK_NOT_FOUND


@pytest.mark.parametrize(
    "msg",
    [
        "unsupported media type for this post",
        "no video formats found",
        "format not available",
        "live event is currently broadcasting",
        "live stream cannot be downloaded",
        "this video is live",
    ],
)
def test_classify_facebook_error_unsupported_media(msg: str):
    assert classify_facebook_error(msg) == texts.FACEBOOK_UNSUPPORTED_MEDIA


@pytest.mark.parametrize(
    "msg",
    [
        "Connection reset by peer",
        "Connection refused",
        "timed out",
        "network is unreachable",
        "read timed out",
        "RemoteDisconnected: Server disconnected",
    ],
)
def test_classify_facebook_error_network(msg: str):
    assert classify_facebook_error(msg) == texts.FACEBOOK_NETWORK_ERROR


def test_classify_facebook_error_empty_and_unknown():
    assert "לא התקבל קובץ מדיה" in classify_facebook_error(None)
    unknown_msg = "some totally novel error happened with post 123"
    classified = classify_facebook_error(unknown_msg)
    assert classified == texts.FACEBOOK_GENERIC_FAILURE
    assert unknown_msg not in classified


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("connection reset by peer", True),
        ("timed out", True),
        ("HTTP Error 500: Internal Server Error", True),
        ("HTTP Error 503: Service Unavailable", True),
        ("login required", False),
        ("This video is no longer available", False),
        ("", False),
        (None, False),
    ],
)
def test_is_retryable_error(msg: str | None, expected: bool):
    assert is_retryable_error(msg) == expected


# --- 6. Progress Formatting ---


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


def test_format_progress_text_finished():
    assert format_progress_text({"status": "finished"}) == texts.PROCESSING


# --- 7. Size Cap Enforcement ---


def test_progress_hook_raises_download_too_large_signal():
    loop = asyncio.new_event_loop()
    try:
        engine = FacebookEngine(max_download_size=10 * 1024 * 1024)
        hook = engine._make_progress_hook(loop)
        hook({"status": "downloading", "total_bytes": 5 * 1024 * 1024})
        with pytest.raises(_DownloadTooLargeSignal):
            hook({"status": "downloading", "total_bytes": 15 * 1024 * 1024})
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_download_too_large_error_surfaces(tmp_path: Path):
    engine = FacebookEngine(max_download_size=10 * 1024 * 1024)

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
        await engine.download("https://www.facebook.com/reel/1127463233306375", dest_dir=tmp_path)


# --- 8. Download Flow Tests ---


@pytest.mark.asyncio
async def test_download_success_single_file(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)
    media_file = tmp_path / "reel [123].mp4"
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
                "id": "123",
                "title": "A great Facebook Reel",
                "requested_downloads": [{"filepath": str(media_file)}],
            }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        result = await engine.download("https://www.facebook.com/reel/123", dest_dir=tmp_path)

    assert result.file_paths == [str(media_file)]
    assert result.title == "A great Facebook Reel"


@pytest.mark.asyncio
async def test_download_success_playlist_multiple_files(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)
    file1 = tmp_path / "a [1].mp4"
    file2 = tmp_path / "b [2].mp4"
    file1.write_bytes(b"one")
    file2.write_bytes(b"two")

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            return {
                "_type": "playlist",
                "title": "Multi-video post",
                "entries": [
                    {"id": "1", "title": "a", "requested_downloads": [{"filepath": str(file1)}]},
                    {"id": "2", "title": "b", "requested_downloads": [{"filepath": str(file2)}]},
                ],
            }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        result = await engine.download("https://www.facebook.com/groups/1/posts/2/", dest_dir=tmp_path)

    assert sorted(result.file_paths) == sorted([str(file1), str(file2)])


@pytest.mark.asyncio
async def test_download_private_content_raises_hebrew_error(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("You must log in to continue")

    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(FacebookDownloadError) as exc_info:
        await engine.download("https://www.facebook.com/reel/999", dest_dir=tmp_path)

    assert texts.FACEBOOK_PRIVATE_OR_LOGIN in str(exc_info.value)


@pytest.mark.asyncio
async def test_download_not_found_raises_hebrew_error(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("This video is no longer available")

    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(FacebookDownloadError) as exc_info:
        await engine.download("https://www.facebook.com/reel/999", dest_dir=tmp_path)

    assert texts.FACEBOOK_NOT_FOUND in str(exc_info.value)


@pytest.mark.asyncio
async def test_download_live_video_raises_unsupported_media(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            return {"id": "live1", "title": "Live now", "is_live": True}

    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(FacebookDownloadError) as exc_info:
        await engine.download("https://www.facebook.com/watch/?v=999", dest_dir=tmp_path)

    assert texts.FACEBOOK_UNSUPPORTED_MEDIA in str(exc_info.value)


@pytest.mark.asyncio
async def test_download_network_error_raises_hebrew_error(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("Connection timed out")

    with patch("yt_dlp.YoutubeDL", _FakeYDL), pytest.raises(FacebookDownloadError) as exc_info:
        await engine.download("https://www.facebook.com/reel/999", dest_dir=tmp_path)

    assert texts.FACEBOOK_NETWORK_ERROR in str(exc_info.value)


@pytest.mark.asyncio
async def test_download_unsupported_url_raises_immediately(tmp_path: Path):
    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)
    with pytest.raises(UnsupportedUrlError):
        await engine.download("https://notfacebook.com/reel/123", dest_dir=tmp_path)


@pytest.mark.asyncio
async def test_download_cancelled_before_start_returns_empty_result(tmp_path: Path):
    from media_bot_v2.engines.base import CancellationToken

    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)
    cancel_token = CancellationToken()
    cancel_token.set()

    result = await engine.download(
        "https://www.facebook.com/reel/123", dest_dir=tmp_path, cancel_token=cancel_token
    )
    assert result.file_paths == []


# --- 9. Redirect Resolution (SSRF guard) ---


class _FakeRedirectResponse:
    def __init__(self, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}


def test_resolve_facebook_url_follows_redirect_chain(monkeypatch):
    calls = []

    def fake_get(url, *, allow_redirects, timeout, headers):
        calls.append(url)
        if url == "https://fb.watch/abc123":
            return _FakeRedirectResponse(302, "https://www.facebook.com/reel/555666777")
        return _FakeRedirectResponse(200)

    monkeypatch.setattr(facebook_engine.requests, "get", fake_get)
    resolved = resolve_facebook_url("https://fb.watch/abc123")
    assert resolved == "https://www.facebook.com/reel/555666777"
    assert len(calls) == 2


def test_resolve_facebook_url_rejects_redirect_to_non_facebook_host(monkeypatch):
    def fake_get(url, *, allow_redirects, timeout, headers):
        if url == "https://fb.watch/evil":
            return _FakeRedirectResponse(302, "http://127.0.0.1/admin")
        raise AssertionError(f"must not follow non-Facebook hop, fetched {url}")

    monkeypatch.setattr(facebook_engine.requests, "get", fake_get)
    resolved = resolve_facebook_url("https://fb.watch/evil")
    # The SSRF hop is never fetched; resolution stops at the last valid URL.
    assert resolved == "https://fb.watch/evil"


def test_resolve_facebook_url_rejects_redirect_to_example_com(monkeypatch):
    def fake_get(url, *, allow_redirects, timeout, headers):
        if url == "https://www.facebook.com/share/r/abc/":
            return _FakeRedirectResponse(302, "https://example.com/not-facebook")
        raise AssertionError(f"must not follow non-Facebook hop, fetched {url}")

    monkeypatch.setattr(facebook_engine.requests, "get", fake_get)
    resolved = resolve_facebook_url("https://www.facebook.com/share/r/abc/")
    assert resolved == "https://www.facebook.com/share/r/abc/"


def test_resolve_facebook_url_strips_tracking_params_but_keeps_v(monkeypatch):
    def fake_get(url, *, allow_redirects, timeout, headers):
        if "fb.watch" in url:
            return _FakeRedirectResponse(
                302, "https://www.facebook.com/watch/?v=123&mibextid=xyz&__cft__[0]=abc&_rdr"
            )
        return _FakeRedirectResponse(200)

    monkeypatch.setattr(facebook_engine.requests, "get", fake_get)
    resolved = resolve_facebook_url("https://fb.watch/abc")
    assert "v=123" in resolved
    assert "mibextid" not in resolved
    assert "__cft__" not in resolved
    assert "_rdr" not in resolved


def test_resolve_facebook_url_noop_for_plain_url():
    url = "https://www.facebook.com/reel/123"
    assert resolve_facebook_url(url) == url


def test_resolve_facebook_url_handles_request_exception(monkeypatch):
    def fake_get(url, *, allow_redirects, timeout, headers):
        raise requests.RequestException("boom")

    monkeypatch.setattr(facebook_engine.requests, "get", fake_get)
    resolved = resolve_facebook_url("https://fb.watch/abc123")
    assert resolved == "https://fb.watch/abc123"


@pytest.mark.asyncio
async def test_download_uses_resolved_url(tmp_path: Path, monkeypatch):
    def fake_get(url, *, allow_redirects, timeout, headers):
        return _FakeRedirectResponse(302, "https://www.facebook.com/reel/555666777")

    monkeypatch.setattr(facebook_engine.requests, "get", fake_get)

    engine = FacebookEngine(max_download_size=100 * 1024 * 1024)
    media_file = tmp_path / "reel [555666777].mp4"
    media_file.write_bytes(b"dummy")
    seen_urls = []

    class _FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def extract_info(self, url, download=True):
            seen_urls.append(url)
            return {
                "id": "555666777",
                "title": "Resolved reel",
                "requested_downloads": [{"filepath": str(media_file)}],
            }

    with patch("yt_dlp.YoutubeDL", _FakeYDL):
        await engine.download("https://fb.watch/abc123", dest_dir=tmp_path)

    assert seen_urls == ["https://www.facebook.com/reel/555666777"]
