"""Tests for M11.14: the six call sites left on Python's default executor
after M11.11's migration to the dedicated thread pool (see executor.py) -
`fetch_title_duration`'s yt-dlp metadata extraction (the one that runs on
every YouTube request, before the download itself) and the five providers'
`_fetch_sync` HTTP calls (tikdownloader, ytmp3, cobalt, musicaldown, tikwm).

Covers the verification round's three asks: (a) each site now runs on the
dedicated `media-bot-worker` pool, not asyncio's unnamed default executor;
(b) `fetch_title_duration`'s `asyncio.wait_for` timeout still cuts off a slow
extraction; (c) N requests above the pool's default size don't deadlock -
the provider fetch path is only ever awaited directly from the event loop,
never from inside a `run_in_thread` worker, so there is no self-wait.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_bot_v2 import executor as executor_module
from media_bot_v2.engines import youtube
from media_bot_v2.providers.cobalt import CobaltProvider
from media_bot_v2.providers.musicaldown import MusicalDownProvider
from media_bot_v2.providers.tikdownloader import TikDownloaderProvider
from media_bot_v2.providers.tikwm import TikWMProvider
from media_bot_v2.providers.ytmp3 import YTmp3Provider


@pytest.fixture(autouse=True)
def _reset_executor_state():
    saved = executor_module._executor
    executor_module._executor = None
    yield
    if executor_module._executor is not None:
        executor_module._executor.shutdown(wait=False)
    executor_module._executor = saved


# ---------------------------------------------------------------------------
# (a) every site now runs on the dedicated media-bot-worker pool
# ---------------------------------------------------------------------------


class _ThreadNameCapturingYDL:
    """Stand-in for yt_dlp.YoutubeDL that records which thread called it."""

    captured_thread_name: str | None = None

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        _ThreadNameCapturingYDL.captured_thread_name = threading.current_thread().name
        return {"title": "t", "duration": 5}


async def test_fetch_title_duration_runs_on_the_dedicated_pool():
    executor_module.init_thread_pool(4)
    _ThreadNameCapturingYDL.captured_thread_name = None
    with patch.object(youtube.yt_dlp, "YoutubeDL", _ThreadNameCapturingYDL):
        result = await youtube.fetch_title_duration("u", opts={}, timeout=5)
    assert result == ("t", "0:05")
    assert _ThreadNameCapturingYDL.captured_thread_name is not None
    assert _ThreadNameCapturingYDL.captured_thread_name.startswith("media-bot-worker")


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    resp.status_code = 200
    return resp


async def test_tikwm_provider_fetch_runs_on_the_dedicated_pool():
    executor_module.init_thread_pool(4)
    names: list[str] = []

    def fake_get(*args, **kwargs):
        names.append(threading.current_thread().name)
        return _mock_response(
            {"code": 0, "data": {"title": "t", "play": "https://x/video.mp4"}}
        )

    with patch("media_bot_v2.providers.tikwm.requests.get", side_effect=fake_get):
        await TikWMProvider().fetch("https://www.tiktok.com/@u/video/1")

    assert names and all(n.startswith("media-bot-worker") for n in names)


async def test_tikdownloader_provider_fetch_runs_on_the_dedicated_pool():
    executor_module.init_thread_pool(4)
    names: list[str] = []
    html_payload = (
        '<a href="https://dl.snapcdn.app/x.mp4">HD Without Watermark</a>'
    )

    def fake_post(*args, **kwargs):
        names.append(threading.current_thread().name)
        return _mock_response({"status": "ok", "data": html_payload})

    with patch("media_bot_v2.providers.tikdownloader.requests.post", side_effect=fake_post):
        await TikDownloaderProvider().fetch("https://www.tiktok.com/@u/video/1")

    assert names and all(n.startswith("media-bot-worker") for n in names)


async def test_musicaldown_provider_fetch_runs_on_the_dedicated_pool():
    executor_module.init_thread_pool(4)
    names: list[str] = []

    def fake_request(method, url, *args, **kwargs):
        names.append(threading.current_thread().name)
        if method == "GET":
            resp = MagicMock()
            resp.text = '<form action="/download"><input type="text" name="url"></form>'
            resp.raise_for_status.return_value = None
            return resp
        resp = MagicMock()
        resp.text = '<a href="https://dl.muscdn.app/x.mp4">Download</a>'
        resp.raise_for_status.return_value = None
        return resp

    with (
        patch("media_bot_v2.providers.musicaldown.requests.Session.get") as mock_get,
        patch("media_bot_v2.providers.musicaldown.requests.Session.post") as mock_post,
    ):
        mock_get.side_effect = lambda url, **kw: fake_request("GET", url, **kw)
        mock_post.side_effect = lambda url, **kw: fake_request("POST", url, **kw)
        await MusicalDownProvider().fetch("https://www.tiktok.com/@u/video/1")

    assert names and all(n.startswith("media-bot-worker") for n in names)


async def test_cobalt_provider_fetch_runs_on_the_dedicated_pool():
    executor_module.init_thread_pool(4)
    names: list[str] = []

    def fake_post(*args, **kwargs):
        names.append(threading.current_thread().name)
        return _mock_response({"status": "tunnel", "url": "https://x/video.mp4", "filename": "video.mp4"})

    with patch("media_bot_v2.providers.cobalt.requests.post", side_effect=fake_post):
        provider = CobaltProvider(instance_url="https://cobalt.example.com")
        await provider.fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert names and all(n.startswith("media-bot-worker") for n in names)


async def test_ytmp3_provider_fetch_runs_on_the_dedicated_pool():
    executor_module.init_thread_pool(4)
    names: list[str] = []

    def fake_get(url, *args, **kwargs):
        names.append(threading.current_thread().name)
        if "/auth" in url:
            return _mock_response({"error": 0, "key": "session-key"})
        if "/init" in url:
            return _mock_response({"error": 0, "convertURL": "https://gamma.gammacloud.net/convert"})
        return _mock_response({"error": 0, "downloadURL": "https://x/video.mp4", "title": "t"})

    with patch("media_bot_v2.providers.ytmp3.requests.get", side_effect=fake_get):
        await YTmp3Provider().fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert names and all(n.startswith("media-bot-worker") for n in names)


# ---------------------------------------------------------------------------
# (b) fetch_title_duration's timeout still cuts off a slow extraction
# ---------------------------------------------------------------------------


async def test_fetch_title_duration_timeout_still_cuts_off_a_slow_source():
    executor_module.init_thread_pool(4)

    class _SlowYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            time.sleep(1.0)
            return {"title": "late", "duration": 5}

    with patch.object(youtube.yt_dlp, "YoutubeDL", _SlowYDL):
        started = asyncio.get_running_loop().time()
        result = await youtube.fetch_title_duration("u", opts={}, timeout=0.05)
        assert result == (None, None)
        assert asyncio.get_running_loop().time() - started < 0.5

        ok = await youtube.fetch_title_duration("u", opts={}, timeout=5)
        assert ok == ("late", "0:05")


# ---------------------------------------------------------------------------
# (c) no deadlock: N requests above the pool's default size run concurrently
# ---------------------------------------------------------------------------


async def test_metadata_lookups_above_pool_size_do_not_deadlock():
    """`fetch_title_duration` is only ever awaited directly from the event
    loop (router handlers), never from inside a `run_in_thread` worker, so
    scheduling more lookups than the pool has workers must simply queue -
    not hang. THREAD_POOL_SIZE=2 with 6 concurrent lookups exercises that
    queuing without a 6-worker pool."""
    executor_module.init_thread_pool(2)

    class _FixedYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            time.sleep(0.05)
            return {"title": "t", "duration": 1}

    with patch.object(youtube.yt_dlp, "YoutubeDL", _FixedYDL):
        results = await asyncio.wait_for(
            asyncio.gather(*(youtube.fetch_title_duration("u", opts={}, timeout=5) for _ in range(6))),
            timeout=5,
        )
    assert results == [("t", "0:01")] * 6


async def test_provider_fetches_above_pool_size_do_not_deadlock():
    """Same shape for the provider side: N provider.fetch() calls awaited
    directly from the event loop, pool sized below N, must all complete
    rather than deadlock waiting on a worker that is itself blocked waiting
    for another worker."""
    executor_module.init_thread_pool(2)

    def fake_get(*args, **kwargs):
        time.sleep(0.05)
        return _mock_response({"code": 0, "data": {"title": "t", "play": "https://x/video.mp4"}})

    with patch("media_bot_v2.providers.tikwm.requests.get", side_effect=fake_get):
        provider = TikWMProvider()
        results = await asyncio.wait_for(
            asyncio.gather(*(provider.fetch("https://www.tiktok.com/@u/video/1") for _ in range(6))),
            timeout=5,
        )
    assert len(results) == 6
    assert all(r.primary_url == "https://x/video.mp4" for r in results)
