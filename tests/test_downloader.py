"""download_provider_media over a local HTTP server fixture, never the real
internet - same spirit as test_direct_engine.py.

Covers the M3 audit finding: max_size enforcement in downloader.py used to be
per-file only, so a TikWM photo slideshow (ProviderResult.media_urls with
several items, each individually small) could sum to far more than the
configured max_download_size without ever tripping the guard.
"""

import asyncio
import http.server
import re
import threading
import time

import pytest

from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError
from media_bot_v2.providers.base import ProviderResult
from media_bot_v2.providers.downloader import download_provider_media

ITEM_CONTENT = b"x" * 100
LARGE_CONTENT = b"y" * 2000


class _FakeProgressReporter:
    """Minimal stand-in for MessageProgressReporter - just records every
    `update()` text so tests can assert on the rendered progress lines."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def update(self, text: str, *, is_terminal: bool = False) -> None:
        self.texts.append(text)


class _RaisingProgressReporter:
    async def update(self, text: str, *, is_terminal: bool = False) -> None:
        raise RuntimeError("progress update boom")


def _percents(texts: list[str]) -> list[int]:
    out = []
    for t in texts:
        m = re.search(r"(\d+)%", t)
        if m:
            out.append(int(m.group(1)))
    return out


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/with-length":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(LARGE_CONTENT)))
            self.end_headers()
            self.wfile.write(LARGE_CONTENT)
            return
        if self.path == "/no-length":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(LARGE_CONTENT)
            return
        if self.path == "/slow":
            # Trickles the body out with a short sleep between chunks, giving
            # a test time to cancel mid-transfer instead of racing a fast
            # loopback response that could finish before cancellation fires.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(LARGE_CONTENT)))
            self.end_headers()
            step = 32
            for i in range(0, len(LARGE_CONTENT), step):
                self.wfile.write(LARGE_CONTENT[i : i + step])
                self.wfile.flush()
                time.sleep(0.02)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(ITEM_CONTENT)

    def log_message(self, *args):
        pass  # keep test output quiet


@pytest.fixture
def local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def _slideshow_result(local_server: str, count: int) -> ProviderResult:
    return ProviderResult(
        provider="tikwm",
        media_urls=[f"{local_server}/photo{i}.jpg" for i in range(count)],
        title="slideshow",
        media_type="photo",
    )


async def test_single_item_under_limit_succeeds(local_server, tmp_path):
    result = await download_provider_media(
        _slideshow_result(local_server, 1), tmp_path, max_size=len(ITEM_CONTENT) + 1
    )
    assert len(result.file_paths) == 1


async def test_cumulative_slideshow_size_is_enforced_across_items(local_server, tmp_path):
    """Each item is well under max_size on its own, but five of them together
    exceed it - this must fail even though no single file ever does."""
    item_count = 5
    max_size = len(ITEM_CONTENT) * 3  # allows 3 items, not all 5

    with pytest.raises(DownloadTooLargeError):
        await download_provider_media(
            _slideshow_result(local_server, item_count), tmp_path, max_size=max_size
        )


async def test_cumulative_slideshow_under_limit_downloads_all_items(local_server, tmp_path):
    item_count = 5
    max_size = len(ITEM_CONTENT) * item_count + 1

    result = await download_provider_media(
        _slideshow_result(local_server, item_count), tmp_path, max_size=max_size
    )
    assert len(result.file_paths) == item_count


async def test_no_limit_means_no_cumulative_enforcement(local_server, tmp_path):
    result = await download_provider_media(
        _slideshow_result(local_server, 5), tmp_path, max_size=None
    )
    assert len(result.file_paths) == 5


def _single_url_result(url: str) -> ProviderResult:
    return ProviderResult(provider="direct", media_urls=[url], title="v", media_type="video")


async def test_reports_progress_bar_and_monotonic_percent_with_content_length(
    monkeypatch, local_server, tmp_path
):
    """M11.8: the provider-fallback stream (used e.g. when the local YouTube
    engine fails over to an extraction provider) used to download with zero
    progress reporting - a small chunk size forces several progress edits so
    the percentage's monotonic rise to 100% is actually exercised."""
    import media_bot_v2.providers.downloader as downloader_module

    monkeypatch.setattr(downloader_module, "_CHUNK_SIZE", 64)
    monkeypatch.setattr(downloader_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    reporter = _FakeProgressReporter()
    result = await download_provider_media(
        _single_url_result(f"{local_server}/with-length"),
        tmp_path,
        max_size=None,
        progress=reporter,
    )
    assert len(result.file_paths) == 1
    assert any("📊 התקדמות" in t for t in reporter.texts)

    percents = _percents(reporter.texts)
    assert percents, "expected at least one bar/percent progress update"
    assert percents == sorted(percents)
    assert percents[-1] == 100


async def test_reports_size_and_speed_without_bar_when_no_content_length(
    monkeypatch, local_server, tmp_path
):
    """No Content-Length -> size+speed only, never a bar frozen at a fake
    percentage (mirrors the existing yt-dlp/format_progress behavior)."""
    import media_bot_v2.providers.downloader as downloader_module

    monkeypatch.setattr(downloader_module, "_CHUNK_SIZE", 64)
    monkeypatch.setattr(downloader_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    reporter = _FakeProgressReporter()
    result = await download_provider_media(
        _single_url_result(f"{local_server}/no-length"),
        tmp_path,
        max_size=None,
        progress=reporter,
    )
    assert len(result.file_paths) == 1
    assert reporter.texts
    assert all("📊 התקדמות" not in t for t in reporter.texts)
    assert any("📥 הורד" in t for t in reporter.texts)


async def test_size_cap_enforced_with_progress_attached(monkeypatch, local_server, tmp_path):
    """Attaching a progress reporter must not weaken the existing size-cap
    enforcement - same error, same cleanup, as without a reporter."""
    import media_bot_v2.providers.downloader as downloader_module

    monkeypatch.setattr(downloader_module, "_CHUNK_SIZE", 64)

    reporter = _FakeProgressReporter()
    with pytest.raises(DownloadTooLargeError):
        await download_provider_media(
            _single_url_result(f"{local_server}/with-length"),
            tmp_path,
            max_size=100,
            progress=reporter,
        )
    assert not any(tmp_path.iterdir())


async def test_cancel_mid_stream_stops_and_reports_no_progress_after(
    monkeypatch, local_server, tmp_path
):
    """Cancelling mid-transfer must stop the download, clean up the partial
    file, and never emit a progress update once cancellation has settled."""
    import media_bot_v2.providers.downloader as downloader_module

    monkeypatch.setattr(downloader_module, "_CHUNK_SIZE", 32)
    monkeypatch.setattr(downloader_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    reporter = _FakeProgressReporter()
    cancel_token = CancellationToken()

    task = asyncio.create_task(
        download_provider_media(
            _single_url_result(f"{local_server}/slow"),
            tmp_path,
            max_size=None,
            cancel_token=cancel_token,
            progress=reporter,
        )
    )
    await asyncio.sleep(0.1)  # let a few slow chunks land and report progress
    assert reporter.texts, "expected progress before cancellation"

    cancel_token.set()
    result = await task
    assert result.file_paths == []
    assert not any(tmp_path.iterdir())  # partial file cleaned up

    await asyncio.sleep(0.05)
    settled_count = len(reporter.texts)
    await asyncio.sleep(0.1)
    assert len(reporter.texts) == settled_count, "no progress update after cancellation settled"


async def test_progress_reporter_failure_does_not_crash_download(
    monkeypatch, local_server, tmp_path
):
    """A progress reporter that always raises must never fail the download
    itself - reporting is UI polish, not part of the download's contract."""
    import media_bot_v2.providers.downloader as downloader_module

    monkeypatch.setattr(downloader_module, "_CHUNK_SIZE", 64)
    monkeypatch.setattr(downloader_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    result = await download_provider_media(
        _single_url_result(f"{local_server}/with-length"),
        tmp_path,
        max_size=None,
        progress=_RaisingProgressReporter(),
    )
    assert len(result.file_paths) == 1
