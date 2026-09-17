"""DirectEngine downloads over a local HTTP server fixture, never the real
internet - this proves the streaming-download code path end-to-end without
violating the "no downloads from the internet" constraint for this stage."""

import http.server
import threading
from pathlib import Path

import pytest

from media_bot_v2.engines.base import DownloadTooLargeError
from media_bot_v2.engines.direct import DirectEngine

FILE_CONTENT = b"hello from a local test server\n" * 100


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
