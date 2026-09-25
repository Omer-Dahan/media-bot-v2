"""M4.2 regression tests: closing the M4.1 independent-verification findings.

Everything that touches the network runs against a local HTTP server on
127.0.0.1 (never the internet), and the size / cancel / retry tests drive the
real yt-dlp through the real engines, because the M4.1 tests only built the
exception objects and so missed that the real download path never reached them.
"""

from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.engines.base import (
    BaseEngine,
    CancellationToken,
    DownloadResult,
    DownloadTooLargeError,
    NotMediaContentError,
    UnsupportedUrlError,
)
from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.instagram import (
    InstagramDownloadError,
    InstagramEngine,
    classify_instagram_error,
)
from media_bot_v2.engines.tiktok import TikTokEngine
from media_bot_v2.engines.youtube import (
    YouTubeDownloadError,
    YouTubeEngine,
    _result_from_info,
    classify_youtube_error,
)
from media_bot_v2.engines.ytdlp_support import TRANSPORT_RETRIES
from media_bot_v2.pipeline import DownloadPipeline
from media_bot_v2.providers.base import BaseProvider, ProviderResult
from media_bot_v2.providers.downloader import download_provider_media
from media_bot_v2.providers.registry import ProviderRegistry
from media_bot_v2.telegram import texts
from tests.test_lessons import _MockProgress, _MockUploader, _setup_test_db

MB = 1024 * 1024


# ==============================================================================
# Local HTTP server
# ==============================================================================


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.get_paths: list[str] = []
        self.bytes_sent = 0
        self.first_get = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    """Routes:
    /f/<size>/<cl|nocl>/<delay_ms>/<name>  - `size` zero bytes, in 64KB chunks,
        with (cl) or without (nocl) a Content-Length, `delay_ms` between chunks.
    /body/<key>/<name>                     - a canned body from `server.bodies`
        as (content_type, bytes).
    """

    protocol_version = "HTTP/1.0"  # no keep-alive: an omitted Content-Length is real

    def log_message(self, *args):
        pass

    def _route(self):
        parts = self.path.lstrip("/").split("/")
        if parts[0] == "f":
            size, mode, delay = int(parts[1]), parts[2], int(parts[3])
            return "video/mp4", None, size, mode == "cl", delay / 1000
        content_type, body = self.server.bodies[parts[1]]  # type: ignore[attr-defined]
        return content_type, body, len(body), True, 0

    def _send_head(self, content_type, length, with_length):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if with_length:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_HEAD(self):
        content_type, _, length, with_length, _ = self._route()
        self._send_head(content_type, length, with_length)

    def do_GET(self):
        state: _State = self.server.state  # type: ignore[attr-defined]
        content_type, body, length, with_length, delay = self._route()
        with state.lock:
            state.get_paths.append(self.path)
        state.first_get.set()
        self._send_head(content_type, length, with_length)
        try:
            if body is not None:
                self.wfile.write(body)
                return
            sent = 0
            chunk = b"\0" * 65536
            while sent < length:
                piece = chunk[: min(len(chunk), length - sent)]
                self.wfile.write(piece)
                sent += len(piece)
                with state.lock:
                    state.bytes_sent += len(piece)
                if delay:
                    time.sleep(delay)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.state = _State()  # type: ignore[attr-defined]
    srv.bodies = {}  # type: ignore[attr-defined]
    srv.base = f"http://127.0.0.1:{srv.server_port}"  # type: ignore[attr-defined]
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


class _PlaylistIE(InfoExtractor):
    """Test-only extractor: `<base>/playlist?items=url1,url2,...` -> a playlist
    whose entries are direct file URLs (what a real YouTube playlist looks like
    to the engine: several entries, each downloaded separately)."""

    _VALID_URL = r"http://127\.0\.0\.1:\d+/playlist"
    IE_NAME = "testplaylist"

    def _real_extract(self, url):
        items = url.split("items=", 1)[1].split(",")
        entries = [
            {"id": f"item{i}", "title": f"item{i}", "url": item, "ext": "mp4"}
            for i, item in enumerate(items)
        ]
        return self.playlist_result(entries, "pl", "Test playlist")


class _YDLWithPlaylist(yt_dlp.YoutubeDL):
    def __init__(self, params=None, auto_init=True):
        super().__init__(params, auto_init)
        ie = _PlaylistIE()
        self.add_info_extractor(ie)
        key = ie.ie_key()
        # The generic extractor claims every URL, so ours has to be tried first.
        self._ies = {key: self._ies[key], **{k: v for k, v in self._ies.items() if k != key}}


def _files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob("*") if p.is_file())


def _no_partials(directory: Path) -> bool:
    return not any(".part" in n or n.endswith((".ytdl", ".temp")) for n in _files(directory))


@pytest.fixture
def youtube_matches_anything():
    """The local test server is not a youtube.com host; everything else about
    `YouTubeEngine.download` stays real."""
    with patch.object(YouTubeEngine, "matches", return_value=True):
        yield


@pytest.fixture
def instagram_matches_anything():
    with patch.object(InstagramEngine, "matches", return_value=True):
        yield


# ==============================================================================
# CRITICAL: partial upload must not become a cache hit
# ==============================================================================


class _CountingTwoPartEngine(BaseEngine):
    def __init__(self) -> None:
        self.calls = 0

    def matches(self, url: str) -> bool:
        return True

    async def download(self, url: str, *, dest_dir: Path) -> DownloadResult:
        self.calls += 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, byte in (("part1.bin", b"A"), ("part2.bin", b"B")):
            p = dest_dir / name
            p.write_bytes(byte * 1000)
            paths.append(str(p))
        return DownloadResult(file_paths=paths, title="TwoPart")


class _FlakyUploader(_MockUploader):
    """Part 2 fails while `fail_part_two` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_part_two = True
        self.cached_sends: list[tuple[str, list[int]]] = []
        self._n = 0

    async def send_file(self, path: Path, *, caption: str | None = None) -> MagicMock:
        self._n += 1
        if self.fail_part_two and path.name == "part2.bin":
            raise RuntimeError("Upload network dropped on part 2")
        return await super().send_file(path, caption=caption)

    async def send_cached(self, archive_chat: str, message_ids: list[int]) -> MagicMock:
        self.cached_sends.append((archive_chat, list(message_ids)))
        return MagicMock()


async def test_partial_upload_is_not_cached_and_repeat_request_downloads_again(tmp_path):
    session_factory, credits_service, _ = _setup_test_db()
    cache = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    engine = _CountingTwoPartEngine()
    uploader = _FlakyUploader()
    key = compute_cache_key("some-video", "720")

    async def run_once(progress: _MockProgress) -> None:
        await pipeline.run(
            user_id=1,
            url="http://example.com/v",
            engine=engine,
            uploader=uploader,
            progress=progress,
            cache=cache,
            cache_key=key,
            archive_channel="@archive",
        )

    # Request 1: part 1 delivered, part 2 fails.
    first = _MockProgress()
    with pytest.raises(RuntimeError):
        await run_once(first)
    assert cache.get(key) is None
    assert texts.DOWNLOAD_DONE not in first.updates

    # Request 2 (same link): must run the download again, not be served the
    # one delivered part from cache and told "done".
    uploader.fail_part_two = False
    second = _MockProgress()
    await run_once(second)
    assert engine.calls == 2
    assert uploader.cached_sends == []
    assert [p.name for p in uploader.sent[-2:]] == ["part1.bin", "part2.bin"]
    assert second.updates[-1] == texts.DOWNLOAD_DONE

    # The complete result *is* cached, with every part, and request 3 is a
    # genuine full hit.
    entry = cache.get(key)
    assert entry is not None
    assert len(entry.message_ids) == 2
    third = _MockProgress()
    await run_once(third)
    assert engine.calls == 2
    assert uploader.cached_sends == [("@archive", entry.message_ids)]


async def test_result_with_a_failed_archive_forward_is_not_cached(tmp_path):
    session_factory, credits_service, _ = _setup_test_db()
    cache = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)

    class _ForwardFailsOnSecond(_FlakyUploader):
        async def forward_to_archive(self, message):
            if message.id == 2:
                raise RuntimeError("archive down")
            return await super().forward_to_archive(message)

    uploader = _ForwardFailsOnSecond()
    uploader.fail_part_two = False
    key = compute_cache_key("v2", "720")
    await pipeline.run(
        user_id=1,
        url="http://example.com/v",
        engine=_CountingTwoPartEngine(),
        uploader=uploader,
        progress=_MockProgress(),
        cache=cache,
        cache_key=key,
        archive_channel="@archive",
    )
    # The user got both parts, but only one is archived - a cache entry would
    # resend a single part later.
    assert cache.get(key) is None


async def test_trimmed_playlist_result_is_not_cached(tmp_path):
    session_factory, credits_service, _ = _setup_test_db()
    cache = VideoCacheStore(session_factory)
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)

    class _TrimmedEngine(_CountingTwoPartEngine):
        async def download(self, url, *, dest_dir):
            res = await super().download(url, dest_dir=dest_dir)
            res.playlist_total, res.playlist_downloaded = 5, 2
            return res

    uploader = _FlakyUploader()
    uploader.fail_part_two = False
    key = compute_cache_key("pl", "720")
    await pipeline.run(
        user_id=1,
        url="http://example.com/pl",
        engine=_TrimmedEngine(),
        uploader=uploader,
        progress=_MockProgress(),
        cache=cache,
        cache_key=key,
        archive_channel="@archive",
    )
    assert cache.get(key) is None


# ==============================================================================
# Finding 3: oversize enforced during a real yt-dlp download
# ==============================================================================

LIMIT = 2 * MB
FILE_SIZE = 6 * MB


@pytest.mark.parametrize("mode", ["cl", "nocl"])
async def test_youtube_real_download_over_limit_reports_size_and_limit(
    server, tmp_path, youtube_matches_anything, mode
):
    """With a Content-Length yt-dlp used to skip the file silently ("no media
    received"); without one it used to download, upload and bill it."""
    engine = YouTubeEngine(quality="720", max_download_size=LIMIT)

    with pytest.raises(DownloadTooLargeError) as exc_info:
        await engine.download(f"{server.base}/f/{FILE_SIZE}/{mode}/0/v.mp4", dest_dir=tmp_path)

    message = str(exc_info.value)
    assert "הקובץ גדול מדי" in message
    assert "2.0MB" in message  # the configured limit
    if mode == "cl":
        assert "6.0MB" in message  # the declared size
    assert exc_info.value.file_size > LIMIT
    assert exc_info.value.max_size == LIMIT
    assert _files(tmp_path) == []


async def test_youtube_real_download_under_limit_still_succeeds(
    server, tmp_path, youtube_matches_anything
):
    engine = YouTubeEngine(quality="720", max_download_size=LIMIT)
    result = await engine.download(f"{server.base}/f/{MB}/nocl/0/v.mp4", dest_dir=tmp_path)
    assert len(result.file_paths) == 1
    assert Path(result.file_paths[0]).stat().st_size == MB


@pytest.mark.parametrize("mode", ["cl", "nocl"])
async def test_instagram_real_download_over_limit_reports_size_and_limit(
    server, tmp_path, instagram_matches_anything, mode
):
    engine = InstagramEngine(max_download_size=LIMIT)
    with pytest.raises(DownloadTooLargeError) as exc_info:
        await engine.download(f"{server.base}/f/{FILE_SIZE}/{mode}/0/v.mp4", dest_dir=tmp_path)
    assert "2.0MB" in str(exc_info.value)
    assert exc_info.value.file_size > LIMIT
    assert _files(tmp_path) == []


@pytest.mark.parametrize("mode", ["cl", "nocl"])
async def test_tiktok_local_real_download_over_limit_reports_size_and_limit(
    server, tmp_path, mode
):
    engine = TikTokEngine(
        registry=ProviderRegistry(),
        health_tracker=MagicMock(),
        max_download_size=LIMIT,
    )
    with pytest.raises(DownloadTooLargeError) as exc_info:
        await asyncio.to_thread(
            engine._download_local_sync,
            f"{server.base}/f/{FILE_SIZE}/{mode}/0/v.mp4",
            tmp_path,
            None,
        )
    assert "2.0MB" in str(exc_info.value)
    assert _files(tmp_path) == []


async def test_youtube_provider_fallback_over_limit_reports_size_and_limit(server, tmp_path):
    """Third YouTube route: a fallback provider's media URL is oversize."""
    _, _, health = _setup_test_db()

    class _Prov(BaseProvider):
        name = "ytmp3"
        supported_platforms = ("youtube",)

        def matches(self, url):
            return True

        async def fetch(self, url):
            return ProviderResult(
                provider="ytmp3", media_urls=[f"{server.base}/f/{FILE_SIZE}/nocl/0/v.mp3"]
            )

    registry = ProviderRegistry(youtube_order=["ytmp3"])
    registry.register(_Prov())
    engine = YouTubeEngine(
        quality="720", max_download_size=LIMIT, registry=registry, health_tracker=health
    )
    with (
        patch.object(engine, "_download_sync", side_effect=YouTubeDownloadError("boom")),
        pytest.raises(DownloadTooLargeError) as exc_info,
    ):
        await engine.download("https://www.youtube.com/watch?v=dQw4w9WgXcQ", dest_dir=tmp_path)
    assert "2.0MB" in str(exc_info.value)
    assert _files(tmp_path) == []


# ==============================================================================
# Finding 1: cancelling a playlist stops the whole playlist and leaves no partials
# ==============================================================================


def _playlist_url(server, items: list[str]) -> str:
    return f"{server.base}/playlist?items=" + ",".join(f"{server.base}{i}" for i in items)


async def test_playlist_cancel_stops_remaining_items_and_removes_partials(
    server, tmp_path, youtube_matches_anything
):
    engine = YouTubeEngine(quality="720", max_download_size=100 * MB, is_playlist=True)
    token = CancellationToken()
    slow_items = [f"/f/{20 * MB}/cl/20/item{i}.mp4" for i in range(5)]
    url = _playlist_url(server, slow_items)

    with patch("media_bot_v2.engines.youtube.yt_dlp.YoutubeDL", _YDLWithPlaylist):
        task = asyncio.create_task(engine.download(url, dest_dir=tmp_path, cancel_token=token))
        assert await asyncio.to_thread(server.state.first_get.wait, 10)
        await asyncio.sleep(0.2)  # mid-download of item 0
        token.set()
        result = await asyncio.wait_for(task, timeout=15)

    assert result.file_paths == []
    await asyncio.sleep(0.3)  # let the server notice the closed connection
    bytes_at_cancel = server.state.bytes_sent
    await asyncio.sleep(0.5)
    # Nothing further is requested or transferred once cancelled: one GET
    # (item 0) ever, none for items 1-4, and the byte counter is frozen.
    assert len(server.state.get_paths) == 1
    assert server.state.bytes_sent == bytes_at_cancel
    assert server.state.bytes_sent < 20 * MB
    assert _files(tmp_path) == []  # no .part / .ytdl left behind


async def test_playlist_skips_oversize_item_and_reports_it(
    server, tmp_path, youtube_matches_anything
):
    engine = YouTubeEngine(quality="720", max_download_size=LIMIT, is_playlist=True)
    url = _playlist_url(
        server,
        [
            f"/f/{MB}/cl/0/a.mp4",
            f"/f/{FILE_SIZE}/nocl/0/big.mp4",
            f"/f/{MB}/cl/0/c.mp4",
        ],
    )
    with patch("media_bot_v2.engines.youtube.yt_dlp.YoutubeDL", _YDLWithPlaylist):
        result = await engine.download(url, dest_dir=tmp_path)

    assert result.playlist_total == 3
    assert result.playlist_downloaded == 2
    assert "1 חרגו ממגבלת הגודל" in (result.playlist_trimmed_reason or "")
    assert _no_partials(tmp_path)


async def test_playlist_where_every_item_is_oversize_raises_too_large(
    server, tmp_path, youtube_matches_anything
):
    engine = YouTubeEngine(quality="720", max_download_size=LIMIT, is_playlist=True)
    url = _playlist_url(server, [f"/f/{FILE_SIZE}/cl/0/a.mp4", f"/f/{FILE_SIZE}/nocl/0/b.mp4"])
    with (
        patch("media_bot_v2.engines.youtube.yt_dlp.YoutubeDL", _YDLWithPlaylist),
        pytest.raises(DownloadTooLargeError) as exc_info,
    ):
        await engine.download(url, dest_dir=tmp_path)
    assert "2.0MB" in str(exc_info.value)
    assert _no_partials(tmp_path)


async def test_direct_engine_cancel_removes_truncated_file_and_stops_transfer(server, tmp_path):
    engine = DirectEngine()
    token = CancellationToken()
    url = f"{server.base}/f/{20 * MB}/cl/20/big.bin"

    task = asyncio.create_task(engine.download(url, dest_dir=tmp_path, cancel_token=token))
    assert await asyncio.to_thread(server.state.first_get.wait, 10)
    await asyncio.sleep(0.2)
    token.set()
    await asyncio.wait_for(task, timeout=15)

    await asyncio.sleep(0.3)  # let the server notice the closed connection
    sent = server.state.bytes_sent
    await asyncio.sleep(0.3)
    assert server.state.bytes_sent == sent
    assert sent < 20 * MB
    assert _files(tmp_path) == []


# ==============================================================================
# Finding 4: whitelist error reporting - nothing internal reaches the user
# ==============================================================================

_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
_POOL_ERROR = (
    "HTTPSConnectionPool(host='vault.corp', port=443): Max retries exceeded "
    "(Caused by NewConnectionError('Failed to establish a new connection'))"
)
_SECRET_MESSAGES = [
    ("db01.corp", "ERROR: unable to connect to db01.corp"),
    ("sk-live-4eC39HqLyjWDarjtT1zdp7dc", "ERROR: bad api key sk-live-4eC39HqLyjWDarjtT1zdp7dc rejected"),
    (_JWT, f"ERROR: token {_JWT} invalid"),
    ("vault.corp", _POOL_ERROR),
    ("/srv/media/tmp/abc123.part", "ERROR: /srv/media/tmp/abc123.part: Permission denied"),
    ("10.20.30.40", "ERROR: proxy 10.20.30.40 refused"),
    ("C:\\svc\\secrets", "ERROR: cannot open C:\\svc\\secrets"),
    ("intranet-gw", "ERROR: handshake with intranet-gw failed"),
]

_FIXED_MESSAGES_YT = {
    texts.YOUTUBE_GENERIC_FAILURE,
    "שגיאת רשת בהורדה מיוטיוב. נסה שוב בעוד מספר רגעים.",
}


@pytest.mark.parametrize(("secret", "raw"), _SECRET_MESSAGES)
def test_youtube_error_classification_never_echoes_raw_text(secret, raw):
    out = classify_youtube_error(raw)
    assert secret not in out
    assert raw not in out


@pytest.mark.parametrize(("secret", "raw"), _SECRET_MESSAGES)
def test_instagram_error_classification_never_echoes_raw_text(secret, raw):
    out = classify_instagram_error(raw)
    assert secret not in out
    assert raw not in out


@pytest.mark.parametrize(("secret", "raw"), _SECRET_MESSAGES)
async def test_instagram_engine_user_message_has_no_internal_details(
    secret, raw, tmp_path, instagram_matches_anything, caplog
):
    class _FailingYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError(raw)

    engine = InstagramEngine(max_download_size=100 * MB)
    with patch("yt_dlp.YoutubeDL", _FailingYDL), pytest.raises(InstagramDownloadError) as exc_info:
        await engine.download("https://www.instagram.com/reel/abc/", dest_dir=tmp_path)
    assert secret not in str(exc_info.value)
    # The details are preserved for operators, in the log.
    if classify_instagram_error(raw) == texts.INSTAGRAM_GENERIC_FAILURE:
        assert any(secret in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(("secret", "raw"), _SECRET_MESSAGES)
async def test_youtube_engine_user_message_has_no_internal_details(
    secret, raw, tmp_path, youtube_matches_anything
):
    class _FailingYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError(raw)

    engine = YouTubeEngine(quality="720", max_download_size=100 * MB)
    with patch("yt_dlp.YoutubeDL", _FailingYDL), pytest.raises(YouTubeDownloadError) as exc_info:
        await engine.download("https://youtu.be/abc12345678", dest_dir=tmp_path)
    assert secret not in str(exc_info.value)


# ==============================================================================
# Finding 6: one attempt per route, everywhere
# ==============================================================================


class _CountingFailingYDL:
    calls = 0

    def __init__(self, opts):
        type(self).opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        type(self).calls += 1
        raise yt_dlp.utils.DownloadError("Connection reset by peer")


@pytest.mark.parametrize("platform", ["youtube", "instagram"])
async def test_transient_network_error_is_attempted_once_per_route(
    platform, tmp_path, youtube_matches_anything, instagram_matches_anything
):
    _CountingFailingYDL.calls = 0
    if platform == "youtube":
        engine = YouTubeEngine(quality="720", max_download_size=100 * MB)
        url, expected = "https://youtu.be/abc12345678", YouTubeDownloadError
    else:
        engine = InstagramEngine(max_download_size=100 * MB)
        url, expected = "https://www.instagram.com/reel/abc/", InstagramDownloadError

    with patch("yt_dlp.YoutubeDL", _CountingFailingYDL), pytest.raises(expected):
        await engine.download(url, dest_dir=tmp_path)

    assert _CountingFailingYDL.calls == 1
    # Transport-level retries live inside yt-dlp, identically for both engines.
    assert _CountingFailingYDL.opts["retries"] == TRANSPORT_RETRIES
    assert _CountingFailingYDL.opts["fragment_retries"] == TRANSPORT_RETRIES


# ==============================================================================
# Finding 9: playlist trimming message states the real reason
# ==============================================================================


def _playlist_info(downloaded: int, total: int) -> dict:
    return {
        "_type": "playlist",
        "title": "PL",
        "playlist_count": total,
        "entries": [{"filepath": f"/tmp/{i}.mp4"} for i in range(downloaded)],
    }


def test_trim_reason_credit_limit_with_untried_items():
    res = _result_from_info(_playlist_info(3, 10), playlist_item_limit=3)
    assert res.playlist_trimmed_reason == "7 לא הורדו עקב מגבלת יתרת הקרדיטים"
    assert "אינם זמינים" not in res.playlist_trimmed_reason


def test_trim_reason_splits_credit_limit_from_failures():
    res = _result_from_info(_playlist_info(2, 10), playlist_item_limit=3)
    assert res.playlist_trimmed_reason == (
        "7 לא הורדו עקב מגבלת יתרת הקרדיטים, 1 אינם זמינים או נכשלו"
    )


def test_trim_reason_without_credit_limit_is_only_failures():
    res = _result_from_info(_playlist_info(6, 10), playlist_item_limit=None)
    assert res.playlist_trimmed_reason == "4 אינם זמינים או נכשלו"


def test_trim_reason_counts_oversize_items_separately():
    res = _result_from_info(_playlist_info(6, 10), playlist_item_limit=None, too_large_count=3)
    assert res.playlist_trimmed_reason == "3 חרגו ממגבלת הגודל, 1 אינם זמינים או נכשלו"


async def test_playlist_trim_message_shown_to_user_lists_all_three_counts(tmp_path):
    _, credits_service, _ = _setup_test_db()
    pipeline = DownloadPipeline(credits_service=credits_service, download_dir=tmp_path)
    progress = _MockProgress()

    class _Engine(BaseEngine):
        def matches(self, url):
            return True

        async def download(self, url, *, dest_dir):
            dest_dir.mkdir(parents=True, exist_ok=True)
            files = []
            for i in range(2):
                p = dest_dir / f"v{i}.mp4"
                p.write_bytes(b"c")
                files.append(str(p))
            info = _playlist_info(2, 10)
            info["entries"] = [{"filepath": f} for f in files]
            res = _result_from_info(info, playlist_item_limit=3)
            res.file_paths = files
            return res

    await pipeline.run(
        user_id=1,
        url="https://www.youtube.com/playlist?list=PL1",
        engine=_Engine(),
        uploader=_MockUploader(),
        progress=progress,
    )
    final = progress.updates[-1]
    assert "הורדו 2 מתוך 10" in final
    assert "7 לא הורדו עקב מגבלת יתרת הקרדיטים" in final
    assert "1 אינם זמינים או נכשלו" in final


# ==============================================================================
# Finding 10: non-media content is rejected on both download paths
# ==============================================================================

_HTML_LIKE_BODIES = {
    "bom_html": ("application/octet-stream", b"\xef\xbb\xbf<!DOCTYPE html><html></html>"),
    "comment_html": ("application/octet-stream", b"<!-- cached -->\n<html><body>x</body></html>"),
    "xml_decl": ("application/octet-stream", b'<?xml version="1.0"?><error>denied</error>'),
    "head_only": ("application/octet-stream", b"  \n<head><title>Blocked</title></head>"),
    "json_object": ("application/octet-stream", b'{"error": "rate limited"}'),
    "json_array": ("application/octet-stream", b'[{"url": "x"}]'),
    "utf16_html": ("application/octet-stream", "<html><body>x</body></html>".encode("utf-16")),
    "ct_json": ("application/json", b'{"a": 1}'),
    "ct_plain": ("text/plain", b"you have been rate limited"),
    "ct_plain_charset": ("text/plain; charset=utf-8", b"just some words"),
    "ct_xml": ("text/xml", b"whatever"),
    "ct_html_binaryish": ("text/html; charset=utf-8", b"\x00\x01\x02\x03" * 10),
    "long_html": ("application/octet-stream", b"<!doctype html>" + b"x" * 5000),
}

_MEDIA_BODIES = {
    "mp4": ("video/mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000),
    "octet_mp4": ("application/octet-stream", b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100),
    "jpeg": ("image/jpeg", b"\xff\xd8\xff\xe0" + b"\x00" * 300),
    "mp3": ("audio/mpeg", b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 500),
}


@pytest.mark.parametrize("key", sorted(_HTML_LIKE_BODIES))
async def test_direct_engine_rejects_non_media_and_leaves_nothing(server, tmp_path, key):
    server.bodies[key] = _HTML_LIKE_BODIES[key]
    with pytest.raises(UnsupportedUrlError) as exc_info:
        await DirectEngine().download(f"{server.base}/body/{key}/file.bin", dest_dir=tmp_path)
    assert "HTML" in str(exc_info.value)
    assert _files(tmp_path) == []


@pytest.mark.parametrize("key", sorted(_MEDIA_BODIES))
async def test_direct_engine_accepts_real_media(server, tmp_path, key):
    server.bodies[key] = _MEDIA_BODIES[key]
    result = await DirectEngine().download(f"{server.base}/body/{key}/file.bin", dest_dir=tmp_path)
    assert Path(result.file_paths[0]).read_bytes() == _MEDIA_BODIES[key][1]


@pytest.mark.parametrize("key", sorted(_HTML_LIKE_BODIES))
async def test_provider_downloader_rejects_non_media_and_leaves_nothing(server, tmp_path, key):
    server.bodies[key] = _HTML_LIKE_BODIES[key]
    provider_result = ProviderResult(
        provider="p", media_urls=[f"{server.base}/body/{key}/v.mp4"], title="t"
    )
    with pytest.raises(NotMediaContentError):
        await download_provider_media(provider_result, tmp_path)
    assert _files(tmp_path) == []


@pytest.mark.parametrize("key", sorted(_MEDIA_BODIES))
async def test_provider_downloader_accepts_real_media(server, tmp_path, key):
    server.bodies[key] = _MEDIA_BODIES[key]
    provider_result = ProviderResult(
        provider="p", media_urls=[f"{server.base}/body/{key}/v.mp4"], title="t"
    )
    result = await download_provider_media(provider_result, tmp_path)
    assert Path(result.file_paths[0]).read_bytes() == _MEDIA_BODIES[key][1]


async def test_provider_returning_a_web_page_falls_through_to_next_provider(server, tmp_path):
    """A provider whose media URL serves a captcha/error page counts as failed
    (not billed, not delivered) and the next provider gets its turn."""
    server.bodies["page"] = _HTML_LIKE_BODIES["bom_html"]
    server.bodies["ok"] = _MEDIA_BODIES["mp4"]
    _, _, health = _setup_test_db()

    def _provider(name: str, key: str) -> BaseProvider:
        class _P(BaseProvider):
            supported_platforms = ("tiktok",)

            def matches(self, url):
                return True

            async def fetch(self, url):
                return ProviderResult(
                    provider=name, media_urls=[f"{server.base}/body/{key}/v.mp4"], title=name
                )

        p = _P()
        p.name = name
        return p

    registry = ProviderRegistry(tiktok_order=["bad", "good"])
    registry.register(_provider("bad", "page"))
    registry.register(_provider("good", "ok"))
    engine = TikTokEngine(registry=registry, health_tracker=health, max_download_size=100 * MB)

    result = await engine.download("https://www.tiktok.com/@u/video/1", dest_dir=tmp_path)
    assert result.title == "good"
    assert len(result.file_paths) == 1
    assert _files(tmp_path) == [Path(result.file_paths[0]).name]
