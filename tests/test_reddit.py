"""Tests for RedditEngine: URL matching, ID extraction, no-network download
flow (HLS/fallback/crosspost), error classification, and token caching.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from media_bot_v2.engines import reddit as reddit_engine
from media_bot_v2.engines.base import DownloadResult
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.reddit import (
    REDDIT_HOSTS,
    RedditDownloadError,
    RedditEngine,
    extract_reddit_id,
    matches_reddit_url,
)
from media_bot_v2.telegram import texts

# --- URL matching ---


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/videos/comments/abc123/some_title/",
        "https://reddit.com/r/videos/comments/abc123",
        "https://old.reddit.com/r/videos/comments/abc123/some_title/",
        "https://reddit.com/comments/abc123",
        "https://redd.it/abc123",
        "https://v.redd.it/abc123",
    ],
)
def test_matches_reddit_url_valid(url: str):
    assert matches_reddit_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/pics",
        "https://www.reddit.com/r/pics/",
        "https://www.reddit.com/user/someuser",
        "https://www.reddit.com/search?q=cats",
        "https://notreddit.com/r/videos/comments/abc123",
        "not-a-valid-url",
        "",
    ],
)
def test_matches_reddit_url_invalid(url: str):
    assert matches_reddit_url(url) is False


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.reddit.com/r/videos/comments/abc123/some_title/", "abc123"),
        ("https://reddit.com/comments/abc123", "abc123"),
        ("https://redd.it/abc123", "abc123"),
        ("https://v.redd.it/abc123", "abc123"),
        ("https://www.reddit.com/r/pics", None),
    ],
)
def test_extract_reddit_id(url: str, expected_id: str | None):
    assert extract_reddit_id(url) == expected_id


def test_reddit_hosts_tuple():
    assert REDDIT_HOSTS == ("reddit.com", "redd.it")


@pytest.mark.parametrize(
    "url",
    [
        "https://v.redd.it/abc123/DASH_720.mp4",
        "https://i.redd.it/abc123.jpg",
    ],
)
def test_matches_reddit_url_rejects_direct_media(url: str):
    assert matches_reddit_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://v.redd.it/abc123/DASH_720.mp4",
        "https://i.redd.it/abc123.jpg",
    ],
)
def test_direct_engine_matches_reddit_media_urls(url: str):
    engine = DirectEngine(max_download_size=4 * 1024 * 1024 * 1024)
    assert engine.matches(url) is True


def test_matches_reddit_url_post_url_still_true():
    assert matches_reddit_url("https://www.reddit.com/r/videos/comments/abc123/slug/") is True
    assert matches_reddit_url("https://redd.it/abc123") is True


# --- helpers ---


def _run(coro):
    return asyncio.run(coro)


def _post_data(
    *,
    title: str = "a reddit video",
    hls_url: str | None = "https://v.redd.it/abc123/HLSPlaylist.m3u8",
    fallback_url: str | None = None,
    crosspost: list | None = None,
    no_video: bool = False,
) -> dict:
    data: dict = {"title": title}
    if crosspost is not None:
        data["crosspost_parent_list"] = crosspost
    if no_video:
        return data
    video: dict = {}
    if hls_url:
        video["hls_url"] = hls_url
    if fallback_url:
        video["fallback_url"] = fallback_url
    data["secure_media"] = {"reddit_video": video}
    return data


@pytest.fixture(autouse=True)
def _reset_token_cache():
    reddit_engine._cached_token = None
    reddit_engine._cached_token_expiry = 0.0
    yield
    reddit_engine._cached_token = None
    reddit_engine._cached_token_expiry = 0.0


def _engine(**overrides) -> RedditEngine:
    kwargs = {
        "max_download_size": 4 * 1024 * 1024 * 1024,
        "client_id": "id",
        "client_secret": "secret",
    }
    kwargs.update(overrides)
    return RedditEngine(**kwargs)


# --- no credentials ---


def test_no_credentials_raises_not_configured(tmp_path: Path):
    engine = RedditEngine(max_download_size=4 * 1024 * 1024 * 1024, client_id=None, client_secret=None)
    with patch.object(reddit_engine, "_fetch_post") as mock_fetch_post:
        with pytest.raises(RedditDownloadError) as exc_info:
            _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))
        assert str(exc_info.value) == texts.REDDIT_NOT_CONFIGURED
        mock_fetch_post.assert_not_called()


# --- hls_url path ---


def test_hls_url_present_downloads_via_hls(tmp_path: Path):
    engine = _engine(max_height=480)
    post = _post_data(hls_url="https://v.redd.it/abc123/HLSPlaylist.m3u8")

    output_path = tmp_path / "hls-download.mp4"
    output_path.write_bytes(b"data")

    with (
        patch.object(reddit_engine, "_fetch_access_token", return_value=("tok", 3600.0)),
        patch.object(reddit_engine, "_fetch_post", return_value=post) as mock_fetch_post,
        patch.object(reddit_engine, "download_hls") as mock_download_hls,
    ):
        mock_download_hls.return_value = DownloadResult(file_paths=[str(output_path)], title="x")
        result = _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))

    mock_download_hls.assert_called_once()
    call_args, call_kwargs = mock_download_hls.call_args
    assert call_args[0] == "https://v.redd.it/abc123/HLSPlaylist.m3u8"
    assert call_kwargs["max_height"] == 480
    assert result.file_paths
    assert result.title == "a reddit video"
    mock_fetch_post.assert_called_once()


# --- fallback_url path ---


def test_fallback_url_used_when_hls_absent(tmp_path: Path):
    engine = _engine()
    post = _post_data(hls_url=None, fallback_url="https://v.redd.it/abc123/DASH_480.mp4")

    def fake_stream_download(self, url, dest_path, cancel_token):
        dest_path.write_bytes(b"video-bytes")

    with (
        patch.object(reddit_engine, "_fetch_access_token", return_value=("tok", 3600.0)),
        patch.object(reddit_engine, "_fetch_post", return_value=post),
        patch.object(RedditEngine, "_stream_download", fake_stream_download),
        patch.object(RedditEngine, "_probe_audio_url", return_value=False),
    ):
        result = _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))

    assert result.file_paths
    assert Path(result.file_paths[0]).exists()


# --- crosspost ---


def test_crosspost_uses_parent_video_outer_title(tmp_path: Path):
    engine = _engine()
    parent = _post_data(title="parent title", hls_url="https://v.redd.it/parent123/HLSPlaylist.m3u8")
    post = _post_data(title="outer crosspost title", hls_url=None, crosspost=[parent])

    output_path = tmp_path / "hls-download.mp4"
    output_path.write_bytes(b"data")

    with (
        patch.object(reddit_engine, "_fetch_access_token", return_value=("tok", 3600.0)),
        patch.object(reddit_engine, "_fetch_post", return_value=post),
        patch.object(reddit_engine, "download_hls") as mock_download_hls,
    ):
        mock_download_hls.return_value = DownloadResult(file_paths=[str(output_path)], title="x")
        result = _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))

    mock_download_hls.assert_called_once()
    assert mock_download_hls.call_args[0][0] == "https://v.redd.it/parent123/HLSPlaylist.m3u8"
    assert result.title == "outer crosspost title"


# --- no video ---


def test_no_video_raises_no_video_error(tmp_path: Path):
    engine = _engine()
    post = _post_data(no_video=True)

    with (
        patch.object(reddit_engine, "_fetch_access_token", return_value=("tok", 3600.0)),
        patch.object(reddit_engine, "_fetch_post", return_value=post),
    ):
        with pytest.raises(RedditDownloadError) as exc_info:
            _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))
        assert str(exc_info.value) == texts.REDDIT_NO_VIDEO


# --- _fetch_post error classification ---


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def test_fetch_post_403_private_or_login():
    with patch("requests.get", return_value=_FakeResponse(403)):
        with pytest.raises(RedditDownloadError) as exc_info:
            reddit_engine._fetch_post("abc123", "tok", "ua", 10, False)
        assert str(exc_info.value) == texts.REDDIT_PRIVATE_OR_LOGIN


def test_fetch_post_404_not_found():
    with patch("requests.get", return_value=_FakeResponse(404)):
        with pytest.raises(RedditDownloadError) as exc_info:
            reddit_engine._fetch_post("abc123", "tok", "ua", 10, False)
        assert str(exc_info.value) == texts.REDDIT_NOT_FOUND


def test_fetch_post_429_rate_limited():
    with patch("requests.get", return_value=_FakeResponse(429)):
        with pytest.raises(RedditDownloadError) as exc_info:
            reddit_engine._fetch_post("abc123", "tok", "ua", 10, False)
        assert str(exc_info.value) == texts.REDDIT_RATE_LIMITED


# --- token caching ---


def test_token_is_cached_across_downloads(tmp_path: Path):
    engine = _engine()
    post = _post_data(hls_url="https://v.redd.it/abc123/HLSPlaylist.m3u8")
    output_path = tmp_path / "hls-download.mp4"
    output_path.write_bytes(b"data")

    with (
        patch.object(
            reddit_engine, "_fetch_access_token", return_value=("tok", 3600.0)
        ) as mock_fetch_token,
        patch.object(reddit_engine, "_fetch_post", return_value=post),
        patch.object(reddit_engine, "download_hls") as mock_download_hls,
    ):
        mock_download_hls.return_value = DownloadResult(file_paths=[str(output_path)], title="x")
        _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))
        output_path.write_bytes(b"data")
        _run(engine.download("https://redd.it/abc123", dest_dir=tmp_path))

    mock_fetch_token.assert_called_once()
