"""DirectEngine <-> HLS wiring: proves the three detection signals (path
extension, MIME-only, buffered #EXTM3U body) route through download_hls
before any destination file is opened, and that a `.m3u8` URL whose body is
not a playlist fails as malformed HLS rather than being saved as a text or
`.bin` file - the bug this item fixes.

All offline against a local HTTP server fixture, never the real internet.
"""

import http.server
import logging
import shutil
import subprocess
import threading

import pytest

from media_bot_v2.engines.direct import DirectEngine
from media_bot_v2.engines.hls import HlsUnsupportedError
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.retry import is_hopeless_failure

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")

MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:1\n"
    "#EXTINF:1.000000,\n"
    "/seg0.ts\n"
    "#EXT-X-ENDLIST\n"
)
LIVE_PLAYLIST = MEDIA_PLAYLIST.replace("#EXT-X-ENDLIST\n", "").replace(
    "#EXT-X-TARGETDURATION:1\n", "#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:42\n"
)

NOT_A_PLAYLIST = "<html><body>this is a web page, not HLS</body></html>"


@pytest.fixture(scope="module")
def segment_bytes(tmp_path_factory):
    """One ~1s H.264/AAC MPEG-TS segment, generated locally with ffmpeg."""
    work = tmp_path_factory.mktemp("hls-wiring-fixture")
    out = work / "seg0.ts"
    subprocess.run(
        [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-f", "mpegts", str(out),
            "-loglevel", "error",
        ],
        check=True,
    )
    return out.read_bytes()


def _make_handler(seg0: bytes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/video.m3u8":
                self._text(MEDIA_PLAYLIST, "application/octet-stream")
            elif self.path == "/live.m3u8":
                self._text(LIVE_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/mime-only-stream":
                # No extension at all: only the HLS Content-Type identifies it.
                self._text(MEDIA_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/seg0.ts":
                self._bytes(seg0, "video/mp2t")
            elif self.path == "/bad.m3u8":
                # `.m3u8` extension, but the body is not a playlist at all -
                # must fail as malformed HLS, never be saved as a file.
                self._text(NOT_A_PLAYLIST, "text/html")
            else:
                self.send_response(404)
                self.end_headers()

        def do_HEAD(self):
            if self.path == "/mime-only-stream":
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.end_headers()
            else:
                self.send_response(501)
                self.end_headers()

        def _text(self, text, content_type):
            self._bytes(text.encode("utf-8"), content_type)

        def _bytes(self, data, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def local_server(segment_bytes):
    server = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(segment_bytes))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_m3u8_extension_routes_to_hls_engine(local_server, tmp_path):
    """A plain `.m3u8` URL never reaches the generic direct-file path - it is
    downloaded and remuxed by the HLS engine into a playable MP4."""
    result = await DirectEngine().download(f"{local_server}/video.m3u8", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    output = result.file_paths[0]
    assert output.endswith(".mp4")
    probe = subprocess.run(  # noqa: ASYNC221 - short-lived local ffprobe check in a test
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type", output],
        check=True, capture_output=True, text=True,
    )
    assert "video" in probe.stdout


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_live_m3u8_routes_to_window_download(local_server, tmp_path):
    result = await DirectEngine().download(f"{local_server}/live.m3u8", dest_dir=tmp_path)
    assert result.file_paths[0].endswith(".mp4")
    assert result.window.selected_duration == pytest.approx(1.0)
    assert result.window.gap_count == 0


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_mime_only_hls_without_extension_routes_to_hls_engine(local_server, tmp_path):
    """No `.m3u8` extension anywhere in the URL - only the HLS Content-Type
    (seen on the HEAD preflight) identifies this as HLS. Before this fix this
    extensionless body fell back to a bare `.bin` file (spec/PLAN-M3U8.md
    section 2)."""
    result = await DirectEngine().download(f"{local_server}/mime-only-stream", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    output = result.file_paths[0]
    assert output.endswith(".mp4")
    assert not output.endswith(".bin")


@pytest.mark.usefixtures("bypass_ssrf_guard")
async def test_malformed_m3u8_body_fails_classified_never_saved_as_file(
    local_server, tmp_path, caplog
):
    """The current bug this item fixes: a `.m3u8` URL whose body is not a
    playlist must fail as malformed HLS, with a classified Hebrew message
    and no retry button - and must never be silently saved as a text/.bin
    file (spec/PLAN-M3U8.md section 2, "what breaks today")."""
    with caplog.at_level(logging.WARNING), pytest.raises(HlsUnsupportedError) as exc_info:
        await DirectEngine().download(f"{local_server}/bad.m3u8", dest_dir=tmp_path)

    assert str(exc_info.value) == texts.HLS_MALFORMED
    assert is_hopeless_failure(exc=exc_info.value)
    assert list(tmp_path.iterdir()) == []

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
