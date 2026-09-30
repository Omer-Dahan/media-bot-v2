"""M11.10 finding D, real-run evidence: a long Hebrew/emoji/mixed title must
not make a real yt-dlp download fail.

With the old `outtmpl` (`%(title).150s [%(id)s].%(ext)s`), `.150s` truncates
by *character* count - a long Hebrew title (2 bytes/char in UTF-8) or an
emoji-heavy one (up to 4 bytes/char) can still build a filename well over
Linux's 255-byte NAME_MAX, and yt-dlp fails the download outright with
`OSError: [Errno 36] File name too long` before a single byte is written.

This drives the real YouTubeEngine end-to-end: real yt-dlp, real filesystem
writes, bytes served by a real local HTTP server (127.0.0.1, never the
internet) - only the metadata extraction step is a test-only InfoExtractor
so the "video" can have an attacker/user-controlled title without needing a
real YouTube video to point at.
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor

from media_bot_v2.engines.youtube import YouTubeEngine

_CONTENT = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000

_HEBREW_TITLE = "כותרת ארוכה מאוד בעברית שמתארת סרטון עם הרבה מילים ותוכן " * 6
_EMOJI_TITLE = "🎉🎬🔥🚀😀🥳🎊✨🌟💥" * 15
_MIXED_TITLE = "וידאו מדהים 😀🚀🎥 with English text mixed in " * 8


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(_CONTENT)))
        self.end_headers()
        self.wfile.write(_CONTENT)

    def log_message(self, *args):
        pass


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


class _FakeIE(InfoExtractor):
    """Test-only extractor returning a synthetic single-video result with a
    caller-controlled title/id, so this file doesn't need a real YouTube
    video to reproduce the long-title failure mode."""

    _VALID_URL = r"http://127\.0\.0\.1:\d+/fake/.*"
    IE_NAME = "m11_10_fake"

    title = "unset"
    video_id = "unset"
    media_url = "unset"

    def _real_extract(self, url):
        return {
            "id": self.video_id,
            "title": self.title,
            "url": self.media_url,
            "ext": "mp4",
        }


class _YDLWithFake(yt_dlp.YoutubeDL):
    def __init__(self, params=None, auto_init=True):
        super().__init__(params, auto_init)
        ie = _FakeIE()
        self.add_info_extractor(ie)
        key = ie.ie_key()
        # The generic extractor claims every URL, so ours has to be tried first.
        self._ies = {key: self._ies[key], **{k: v for k, v in self._ies.items() if k != key}}


@pytest.mark.parametrize(
    "title",
    [_HEBREW_TITLE, _EMOJI_TITLE, _MIXED_TITLE],
    ids=["hebrew", "emoji", "mixed"],
)
async def test_youtube_engine_survives_long_title_over_real_yt_dlp(local_server, tmp_path, title):
    video_id = "abc123XYZ9"
    _FakeIE.title = title
    _FakeIE.video_id = video_id
    _FakeIE.media_url = f"{local_server}/video.mp4"

    engine = YouTubeEngine(quality="720", max_download_size=100 * 1024 * 1024)

    with (
        patch.object(YouTubeEngine, "matches", return_value=True),
        patch("media_bot_v2.engines.youtube.yt_dlp.YoutubeDL", _YDLWithFake),
    ):
        result = await engine.download(f"{local_server}/fake/{video_id}", dest_dir=tmp_path)

    assert len(result.file_paths) == 1
    written = Path(result.file_paths[0])
    assert written.exists()
    assert written.read_bytes() == _CONTENT
    assert len(written.name.encode("utf-8")) <= 255
    assert video_id in written.name
    assert written.suffix == ".mp4"
