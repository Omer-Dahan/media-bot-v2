"""DirectEngine downloads over a local HTTP server fixture, never the real
internet - this proves the streaming-download code path end-to-end without
violating the "no downloads from the internet" constraint for this stage."""

import http.server
import re
import threading
import time
from pathlib import Path

import pytest

from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError, UnsupportedUrlError
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
        if self.path == "/redirect":
            # 302s to a path with a different name and no Content-Disposition -
            # the saved filename must come from this final destination, not
            # from "/redirect" itself.
            self.send_response(302)
            self.send_header("Location", "/final-destination.pdf")
            self.end_headers()
            return
        if self.path == "/redirect-with-cd":
            self.send_response(302)
            self.send_header("Location", "/final-with-cd")
            self.end_headers()
            return
        if self.path == "/final-with-cd":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="override-name.bin"')
            self.end_headers()
            self.wfile.write(FILE_CONTENT)
            return
        if self.path == "/redirect-chain-1":
            self.send_response(302)
            self.send_header("Location", "/redirect-chain-2")
            self.end_headers()
            return
        if self.path == "/redirect-chain-2":
            self.send_response(302)
            self.send_header("Location", "/final-destination.pdf")
            self.end_headers()
            return
        if self.path == "/redirect-to-dir":
            # Final URL's path ends in "/" - no filename to extract at all.
            self.send_response(302)
            self.send_header("Location", "/some-dir/")
            self.end_headers()
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


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_download_streams_file_to_disk(local_server, tmp_path):
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    downloaded = tmp_path / "test-file.bin"
    assert downloaded.exists()
    assert downloaded.read_bytes() == FILE_CONTENT


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_download_raises_on_http_error(local_server, tmp_path):
    import requests

    with pytest.raises(requests.HTTPError):
        await DirectEngine().download(f"{local_server}/does-not-exist", dest_dir=tmp_path)


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_download_stops_and_deletes_file_when_exceeding_max_size(local_server, tmp_path):
    """Covers finding 5: an oversized (or endless) response must not be
    allowed to fill the disk - it should stop as soon as it crosses the
    configured limit and leave nothing behind."""
    engine = DirectEngine(max_download_size=100)  # FILE_CONTENT is far bigger than this

    with pytest.raises(DownloadTooLargeError):
        await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)

    assert not any(tmp_path.iterdir())  # no partial file left on disk


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_download_allows_file_under_max_size(local_server, tmp_path):
    engine = DirectEngine(max_download_size=len(FILE_CONTENT) + 1)
    result = await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)
    assert Path(result.file_paths[0]).read_bytes() == FILE_CONTENT


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_download_rejects_declared_content_length_before_writing_any_bytes(
    local_server, tmp_path
):
    """A response that declares an oversized Content-Length must fail
    immediately, before streaming gigabytes to disk in the background."""
    engine = DirectEngine(max_download_size=100)

    with pytest.raises(DownloadTooLargeError):
        await engine.download(f"{local_server}/declared-oversized", dest_dir=tmp_path)

    assert not any(tmp_path.iterdir())  # no file was ever opened for writing


@pytest.mark.usefixtures("bypass_ssrf_guard")
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


@pytest.mark.usefixtures("bypass_ssrf_guard")
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


@pytest.mark.usefixtures("bypass_ssrf_guard")
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


@pytest.mark.usefixtures("bypass_ssrf_guard")
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


@pytest.mark.usefixtures("bypass_ssrf_guard")
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


# --- Finding A: path traversal via a percent-encoded filename -------------


@pytest.mark.usefixtures("bypass_ssrf_guard")
@pytest.mark.parametrize(
    "malicious_path",
    [
        "/..%2F..%2Fescaped.bin",
        "/%2Fetc%2Fpasswd",
        "/..%5C..%5Cescaped.bin",  # ..\..\ (Windows-style separator)
        "/%2Fetc%2F%2E%2E%2Fescaped.bin",
        "/etc%2Fpasswd",
        "/foo%00bar.bin",  # embedded control/NUL char
        "/" + "%2E" * 50 + "escaped.bin",  # long run of encoded dots
    ],
)
async def test_download_confines_malicious_filename_inside_dest_dir(
    local_server, tmp_path, malicious_path
):
    """Covers finding A: a `%2F`/`..` payload in the URL path must never
    place the downloaded file outside dest_dir - the file must land inside
    tmp_path, and nothing must exist outside it."""
    engine = DirectEngine()
    result = await engine.download(f"{local_server}{malicious_path}", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    written = Path(result.file_paths[0]).resolve()
    assert written.parent == tmp_path.resolve()
    assert written.is_relative_to(tmp_path.resolve())
    assert written.read_bytes() == FILE_CONTENT
    # Nothing was written outside dest_dir (e.g. two levels up).
    assert not (tmp_path.parent.parent / "escaped.bin").exists()


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_download_empty_decoded_name_falls_back_to_default(local_server, tmp_path):
    """`%2F%2F` decodes to an empty name after basename extraction - must
    fall back to a default filename inside dest_dir, not an empty/invalid
    path."""
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/%2F%2F", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    written = Path(result.file_paths[0])
    assert written.name  # non-empty
    assert written.parent.resolve() == tmp_path.resolve()
    assert written.exists()


# --- Finding B: SSRF ---------------------------------------------------


async def test_download_blocks_loopback_target(local_server, tmp_path):
    """The real guard (no bypass fixture here) must refuse to fetch from
    127.0.0.1 - exactly what `local_server` is - proving the engine itself
    enforces the SSRF guard end-to-end, not just the guard in isolation."""
    engine = DirectEngine()
    with pytest.raises(UnsupportedUrlError):
        await engine.download(f"{local_server}/test-file.bin", dest_dir=tmp_path)
    assert not any(tmp_path.iterdir())


async def test_download_blocks_disallowed_scheme(tmp_path):
    engine = DirectEngine()
    with pytest.raises(UnsupportedUrlError):
        await engine.download("file:///etc/passwd", dest_dir=tmp_path)


# --- Filename derived from the final (post-redirect) URL, not the original -


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_redirect_without_content_disposition_uses_final_url_filename(
    local_server, tmp_path
):
    """A redirect to a URL with no Content-Disposition must name the file
    after the final destination's path, not the originally-requested URL."""
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/redirect", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    assert Path(result.file_paths[0]).name == "final-destination.pdf"


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_redirect_with_content_disposition_prefers_header(local_server, tmp_path):
    """Content-Disposition on the final response still wins over the final
    URL's own path."""
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/redirect-with-cd", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    assert Path(result.file_paths[0]).name == "override-name.bin"


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_redirect_chain_uses_final_destination_filename(local_server, tmp_path):
    """Several hops deep, the name must still come from the very last
    destination, not an intermediate hop or the original URL."""
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/redirect-chain-1", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    assert Path(result.file_paths[0]).name == "final-destination.pdf"


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_redirect_to_nameless_final_url_falls_back_to_default(local_server, tmp_path):
    """The final URL's path ends in "/" - there's no filename to extract -
    must fall back to the default name, not raise or produce an empty one."""
    engine = DirectEngine()
    result = await engine.download(f"{local_server}/redirect-to-dir", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    name = Path(result.file_paths[0]).name
    assert name  # non-empty
    assert Path(name).stem == "download"
