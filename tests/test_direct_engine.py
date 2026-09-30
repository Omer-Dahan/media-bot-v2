"""DirectEngine downloads over a local HTTP server fixture, never the real
internet - this proves the streaming-download code path end-to-end without
violating the "no downloads from the internet" constraint for this stage."""

import http.server
import re
import threading
import time
from pathlib import Path

import pytest

from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError
from media_bot_v2.engines.direct import DirectEngine

FILE_CONTENT = b"hello from a local test server\n" * 100
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
        if self.path == "/does-not-exist":
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/declared-oversized":
            # Declares a huge Content-Length but only ever sends a few bytes -
            # proves the engine rejects based on the header alone, before
            # reading (let alone writing) any of the body.
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "999999999")
            self.end_headers()
            self.wfile.write(b"short")
            return
        if self.path == "/large":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(LARGE_CONTENT)))
            self.end_headers()
            self.wfile.write(LARGE_CONTENT)
            return
        if self.path == "/large-no-length":
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
        self.wfile.write(FILE_CONTENT)

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


def test_matches_http_and_https_urls():
    engine = DirectEngine()
    assert engine.matches("http://example.local/file.bin")
    assert engine.matches("https://example.local/file.bin")
    assert not engine.matches("ftp://example.local/file.bin")
    assert not engine.matches("not a url")


async def test_download_streams_file_to_disk(local_server, tmp_path):
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    downloaded = tmp_path / "test-file.bin"
    assert downloaded.exists()
    assert downloaded.read_bytes() == FILE_CONTENT


async def test_download_raises_on_http_error(local_server, tmp_path):
    import requests

    with pytest.raises(requests.HTTPError):
        await DirectEngine().download(f"{local_server}/does-not-exist", dest_dir=tmp_path)


async def test_download_stops_and_deletes_file_when_exceeding_max_size(local_server, tmp_path):
    """Covers finding 5: an oversized (or endless) response must not be
    allowed to fill the disk - it should stop as soon as it crosses the
    configured limit and leave nothing behind."""
    engine = DirectEngine(max_download_size=100)  # FILE_CONTENT is far bigger than this

    with pytest.raises(DownloadTooLargeError):
        await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)

    assert not any(tmp_path.iterdir())  # no partial file left on disk


async def test_download_allows_file_under_max_size(local_server, tmp_path):
    engine = DirectEngine(max_download_size=len(FILE_CONTENT) + 1)
    result = await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)
    assert Path(result.file_paths[0]).read_bytes() == FILE_CONTENT


async def test_download_rejects_declared_content_length_before_writing_any_bytes(
    local_server, tmp_path
):
    """A response that declares an oversized Content-Length must fail
    immediately, before streaming gigabytes to disk in the background."""
    engine = DirectEngine(max_download_size=100)

    with pytest.raises(DownloadTooLargeError):
        await engine.download(f"{local_server}/declared-oversized", dest_dir=tmp_path)

    assert not any(tmp_path.iterdir())  # no file was ever opened for writing


async def test_reports_progress_bar_and_monotonic_percent_with_content_length(
    monkeypatch, local_server, tmp_path
):
    """M11.8: the direct-link engine used to download with zero progress
    reporting - a small chunk size forces several progress edits so the
    percentage's monotonic rise to 100% is actually exercised."""
    import media_bot_v2.engines.direct as direct_module

    monkeypatch.setattr(direct_module, "_CHUNK_SIZE", 64)
    monkeypatch.setattr(direct_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    reporter = _FakeProgressReporter()
    engine = DirectEngine(progress=reporter)
    result = await engine.download(f"{local_server}/large", dest_dir=tmp_path)

    assert Path(result.file_paths[0]).read_bytes() == LARGE_CONTENT
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
    import media_bot_v2.engines.direct as direct_module

    monkeypatch.setattr(direct_module, "_CHUNK_SIZE", 64)
    monkeypatch.setattr(direct_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    reporter = _FakeProgressReporter()
    engine = DirectEngine(progress=reporter)
    result = await engine.download(f"{local_server}/large-no-length", dest_dir=tmp_path)

    assert Path(result.file_paths[0]).read_bytes() == LARGE_CONTENT
    assert reporter.texts
    assert all("📊 התקדמות" not in t for t in reporter.texts)
    assert any("📥 הורד" in t for t in reporter.texts)


async def test_size_cap_enforced_with_progress_attached(monkeypatch, local_server, tmp_path):
    """Attaching a progress reporter must not weaken the existing size-cap
    enforcement - same error, same cleanup, as without a reporter."""
    import media_bot_v2.engines.direct as direct_module

    monkeypatch.setattr(direct_module, "_CHUNK_SIZE", 64)

    reporter = _FakeProgressReporter()
    engine = DirectEngine(max_download_size=100, progress=reporter)

    with pytest.raises(DownloadTooLargeError):
        await engine.download(f"{local_server}/large", dest_dir=tmp_path)

    assert not any(tmp_path.iterdir())


async def test_cancel_mid_stream_stops_and_reports_no_progress_after(
    monkeypatch, local_server, tmp_path
):
    """Cancelling mid-transfer must stop the download, clean up the partial
    file, and never emit a progress update once cancellation has settled."""
    import asyncio

    import media_bot_v2.engines.direct as direct_module

    monkeypatch.setattr(direct_module, "_CHUNK_SIZE", 32)
    monkeypatch.setattr(direct_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    reporter = _FakeProgressReporter()
    cancel_token = CancellationToken()
    engine = DirectEngine(progress=reporter)

    task = asyncio.create_task(
        engine.download(f"{local_server}/slow", dest_dir=tmp_path, cancel_token=cancel_token)
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
    import media_bot_v2.engines.direct as direct_module

    monkeypatch.setattr(direct_module, "_CHUNK_SIZE", 64)
    monkeypatch.setattr(direct_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)

    engine = DirectEngine(progress=_RaisingProgressReporter())
    result = await engine.download(f"{local_server}/large", dest_dir=tmp_path)

    assert Path(result.file_paths[0]).read_bytes() == LARGE_CONTENT
