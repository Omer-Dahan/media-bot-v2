"""DirectEngine downloads over a local HTTP server fixture, never the real
internet - this proves the streaming-download code path end-to-end without
violating the "no downloads from the internet" constraint for this stage."""

import http.server
import threading

import pytest

from media_bot_v2.engines.direct import DirectEngine

FILE_CONTENT = b"hello from a local test server\n" * 100


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/does-not-exist":
            self.send_response(404)
            self.end_headers()
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
