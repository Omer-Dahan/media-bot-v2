"""download_provider_media over a local HTTP server fixture, never the real
internet - same spirit as test_direct_engine.py.

Covers the M3 audit finding: max_size enforcement in downloader.py used to be
per-file only, so a TikWM photo slideshow (ProviderResult.media_urls with
several items, each individually small) could sum to far more than the
configured max_download_size without ever tripping the guard.
"""

import http.server
import threading

import pytest

from media_bot_v2.engines.base import DownloadTooLargeError
from media_bot_v2.providers.base import ProviderResult
from media_bot_v2.providers.downloader import download_provider_media

ITEM_CONTENT = b"x" * 100


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
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
