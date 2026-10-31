"""HLS engine core tests: playlist detection/parsing, guarded fetch, local
rewrite, and local ffmpeg remux - all offline against a local HTTP server
fixture, never the real internet.

Media fixtures (two short H.264/AAC MPEG-TS segments) are generated with
the installed ffmpeg at test time, never downloaded.
"""

import http.server
import shutil
import subprocess
import threading

import pytest
import requests

from media_bot_v2.engines import hls
from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError
from media_bot_v2.engines.ssrf_guard import safe_request as real_safe_request

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")

MASTER_PLAYLIST = (
    "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=160x120\nvariant/index.m3u8\n"
)

MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:1\n"
    "#EXTINF:1.000000,\n"
    "seg0.ts\n"
    "#EXTINF:1.000000,\n"
    "seg1.ts\n"
    "#EXT-X-ENDLIST\n"
)

LIVE_PLAYLIST = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.000000,\nseg0.ts\n"

MALFORMED_PLAYLIST = "not a playlist at all\n"


@pytest.fixture(scope="module")
def ts_segments(tmp_path_factory):
    """Two ~1s H.264/AAC MPEG-TS segments, generated locally with ffmpeg."""
    work = tmp_path_factory.mktemp("hls-fixture")
    source = work / "source.ts"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-keyint_min",
            "10",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(source),
            "-loglevel",
            "error",
        ],
        check=True,
    )
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            "1",
            "-reset_timestamps",
            "1",
            str(work / "seg%d.ts"),
            "-loglevel",
            "error",
        ],
        check=True,
    )
    return (work / "seg0.ts").read_bytes(), (work / "seg1.ts").read_bytes()


def _make_handler(seg0: bytes, seg1: bytes, request_log: list[str]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            request_log.append(self.path)
            if self.path == "/master.m3u8":
                self._text(MASTER_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/variant/index.m3u8":
                self._text(MEDIA_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/variant/seg0.ts":
                self._bytes(seg0, "video/mp2t")
            elif self.path == "/variant/seg1.ts":
                self._bytes(seg1, "video/mp2t")
            elif self.path == "/live.m3u8":
                self._text(LIVE_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/malformed.m3u8":
                self._text(MALFORMED_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/opaque-signature":
                # No extension, generic content type: only the #EXTM3U body
                # signature identifies this as HLS.
                self._text(
                    MEDIA_PLAYLIST.replace("seg0.ts", "/variant/seg0.ts").replace(
                        "seg1.ts", "/variant/seg1.ts"
                    ),
                    "application/octet-stream",
                )
            else:
                self.send_response(404)
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
def local_server(ts_segments):
    seg0, seg1 = ts_segments
    request_log: list[str] = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(seg0, seg1, request_log))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", request_log
    finally:
        server.shutdown()
        thread.join()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_is_hls_path_case_insensitive_ignores_query():
    assert hls.is_hls_path("https://example.com/video.M3U8?token=abc")
    assert hls.is_hls_path("https://example.com/a/b/c.m3u8")
    assert not hls.is_hls_path("https://example.com/video.m3u8x")
    assert not hls.is_hls_path("https://example.com/video.mp4")


def test_detect_extensionless_hls_by_content_type():
    assert hls.detect_hls(
        "https://example.com/watch", content_type="application/vnd.apple.mpegurl; charset=utf-8"
    )
    assert hls.detect_hls("https://example.com/watch", content_type="audio/x-mpegurl")
    assert not hls.detect_hls("https://example.com/watch", content_type="application/octet-stream")


def test_detect_extensionless_hls_by_body_signature():
    assert hls.detect_hls(
        "https://example.com/opaque",
        content_type="application/octet-stream",
        body_head=b"#EXTM3U\n#EXT-X-VERSION:3\n",
    )
    assert hls.detect_hls(
        "https://example.com/opaque", content_type=None, body_head=b"\xef\xbb\xbf  \n#EXTM3U\n"
    )
    assert not hls.detect_hls(
        "https://example.com/opaque", content_type="application/octet-stream", body_head=b"not hls"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_malformed_playlist_raises_malformed():
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.parse_playlist(MALFORMED_PLAYLIST)
    assert excinfo.value.kind == "malformed"


def test_parse_master_and_media_playlist():
    master = hls.parse_playlist(MASTER_PLAYLIST)
    assert isinstance(master, hls.MasterPlaylist)
    assert master.variants[0].height == 120
    assert master.variants[0].uri == "variant/index.m3u8"

    media = hls.parse_playlist(MEDIA_PLAYLIST)
    assert isinstance(media, hls.MediaPlaylist)
    assert media.has_endlist
    assert [s.uri for s in media.segments] == ["seg0.ts", "seg1.ts"]


def test_parse_live_playlist_has_no_endlist():
    media = hls.parse_playlist(LIVE_PLAYLIST)
    assert isinstance(media, hls.MediaPlaylist)
    assert not media.has_endlist


def test_parse_attributes_honours_quoted_commas():
    attrs = hls._parse_attributes(
        'BANDWIDTH=500000,CODECS="avc1.4d001f,mp4a.40.2",RESOLUTION=160x120'
    )
    assert attrs["CODECS"] == "avc1.4d001f,mp4a.40.2"
    assert attrs["RESOLUTION"] == "160x120"


# ---------------------------------------------------------------------------
# Full download: finite master+media playlist, relative segment URIs
# ---------------------------------------------------------------------------


def test_download_hls_master_and_media_playlist_produces_playable_mp4(
    local_server, tmp_path, bypass_ssrf_guard
):
    base_url, request_log = local_server
    result = hls.download_hls(f"{base_url}/master.m3u8", tmp_path)

    assert len(result.file_paths) == 1
    output = result.file_paths[0]
    assert output.endswith(".mp4")

    probe = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,codec_type",
            "-of",
            "default=noprint_wrappers=1",
            output,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "codec_name=h264" in probe.stdout
    assert "codec_name=aac" in probe.stdout
    duration = float(
        next(
            line.split("=")[1] for line in probe.stdout.splitlines() if line.startswith("duration=")
        )
    )
    assert 1.5 < duration < 2.5

    # Relative segment URIs were resolved against the media playlist's own
    # final URL, not the original master URL.
    assert "/master.m3u8" in request_log
    assert "/variant/index.m3u8" in request_log
    assert "/variant/seg0.ts" in request_log
    assert "/variant/seg1.ts" in request_log

    # No leftover intermediate files in the task directory.
    remaining = {p.name for p in tmp_path.iterdir()}
    assert remaining == {output.rsplit("/", 1)[-1]}


def test_extensionless_hls_url_is_downloaded_via_signature(
    local_server, tmp_path, bypass_ssrf_guard
):
    base_url, _ = local_server
    result = hls.download_hls(f"{base_url}/opaque-signature", tmp_path)
    assert len(result.file_paths) == 1
    assert result.file_paths[0].endswith(".mp4")


# ---------------------------------------------------------------------------
# Unsupported / malformed
# ---------------------------------------------------------------------------


def test_live_playlist_with_only_expired_segment_raises_window_expired(
    local_server, tmp_path, bypass_ssrf_guard
):
    base_url, _ = local_server
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls(f"{base_url}/live.m3u8", tmp_path)
    assert excinfo.value.kind == "window_expired"
    assert list(tmp_path.iterdir()) == []


def test_malformed_playlist_raises_unsupported_malformed(local_server, tmp_path, bypass_ssrf_guard):
    base_url, _ = local_server
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls(f"{base_url}/malformed.m3u8", tmp_path)
    assert excinfo.value.kind == "malformed"
    assert list(tmp_path.iterdir()) == []


def test_download_too_large_leaves_no_partial_files(local_server, tmp_path, bypass_ssrf_guard):
    base_url, _ = local_server
    with pytest.raises(DownloadTooLargeError):
        hls.download_hls(f"{base_url}/master.m3u8", tmp_path, max_size=10)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Every fetch goes through the SSRF guard
# ---------------------------------------------------------------------------


def test_every_fetch_goes_through_the_guard(local_server, tmp_path, monkeypatch, bypass_ssrf_guard):
    base_url, _ = local_server
    calls: list[str] = []

    def recording_safe_request(method, url, **kwargs):
        calls.append(url)
        return real_safe_request(method, url, **kwargs)

    monkeypatch.setattr(hls, "safe_request", recording_safe_request)

    hls.download_hls(f"{base_url}/master.m3u8", tmp_path)

    assert f"{base_url}/master.m3u8" in calls
    assert f"{base_url}/variant/index.m3u8" in calls
    assert f"{base_url}/variant/seg0.ts" in calls
    assert f"{base_url}/variant/seg1.ts" in calls
    assert len(calls) == 4


def _response(url: str, body: bytes, status: int = 200, **headers: str) -> requests.Response:
    response = requests.Response()
    response.url = url
    response.status_code = status
    response._content = body
    response._content_consumed = True
    response.headers.update(headers)
    response.close = lambda: None
    return response


def _encrypt_segment(path, clear: bytes, key: bytes, iv: bytes) -> bytes:
    clear_path = path.with_suffix(".clear")
    clear_path.write_bytes(clear)
    subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
            "-nosalt",
            "-in",
            str(clear_path),
            "-out",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def test_aes128_key_rotation_explicit_and_sequence_iv(
    ts_segments, tmp_path, monkeypatch, bypass_ssrf_guard
):
    key_one = bytes(range(16))
    key_two = bytes(reversed(range(16)))
    explicit_iv = bytes.fromhex("11223344556677889900aabbccddeeff")
    derived_iv = (8).to_bytes(16, "big")
    encrypted = [
        _encrypt_segment(tmp_path / "enc0.ts", ts_segments[0], key_one, explicit_iv),
        _encrypt_segment(tmp_path / "enc1.ts", ts_segments[1], key_two, derived_iv),
    ]
    playlist = (
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://keys.test/one",'
        "IV=11223344556677889900aabbccddeeff\n"
        "#EXTINF:1,\nseg0.ts\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://keys.test/two"\n'
        "#EXTINF:1,\nseg1.ts\n#EXT-X-ENDLIST\n"
    )
    request_log: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            request_log.append(self.path)
            bodies = {
                "/index.m3u8": playlist.encode(),
                "/seg0.ts": encrypted[0],
                "/seg1.ts": encrypted[1],
            }
            body = bodies.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            if body is not None:
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key_calls: list[str] = []
    original = hls.safe_request

    def key_aware_request(method, url, **kwargs):
        if url.startswith("https://keys.test/"):
            key_calls.append(url)
            body = key_one if url.endswith("/one") else key_two
            return _response(url, body)
        return original(method, url, **kwargs)

    monkeypatch.setattr(hls, "safe_request", key_aware_request)
    try:
        result = hls.download_hls(
            f"http://127.0.0.1:{server.server_port}/index.m3u8", tmp_path / "out"
        )
    finally:
        server.shutdown()
        thread.join()

    assert key_calls.count("https://keys.test/one") == 1
    assert key_calls.count("https://keys.test/two") == 1
    probe = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name",
            "-of",
            "default=noprint_wrappers=1",
            result.file_paths[0],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "codec_name=h264" in probe.stdout
    assert "codec_name=aac" in probe.stdout
    assert float(next(x.split("=")[1] for x in probe.stdout.splitlines() if x.startswith("duration="))) > 1.5
    assert {path.name for path in (tmp_path / "out").iterdir()} == {"hls-download.mp4"}


def test_http_key_is_refused_without_fetch(tmp_path, monkeypatch):
    playlist_url = "https://media.test/index.m3u8"
    playlist = (
        b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="http://keys.test/key"\n'
        b"#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    )
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        if url == playlist_url:
            return _response(url, playlist)
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(hls, "safe_request", fake_request)
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls(playlist_url, tmp_path)
    assert excinfo.value.kind == "https_key_required"
    assert calls == [playlist_url]
    assert list(tmp_path.iterdir()) == []


def test_ignored_byte_range_fails_before_writing(tmp_path, monkeypatch):
    response = _response("https://media.test/file.ts", b"x" * 100)
    monkeypatch.setattr(hls, "_guarded_get", lambda *args, **kwargs: response)
    with pytest.raises(hls.HlsDownloadError):
        hls._fetch_to_file(
            "https://media.test/file.ts",
            tmp_path / "seg.ts",
            hls._ByteCounter(None),
            None,
            byterange=(10, 20),
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("documents", "kwargs", "kind"),
    [
        (
            {
                "https://media.test/a.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nb.m3u8\n",
                "https://media.test/b.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nc.m3u8\n",
            },
            {"max_playlist_depth": 1},
            "playlist_depth_exceeded",
        ),
        (
            {"https://media.test/a.m3u8": "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n"},
            {},
            "playlist_cycle",
        ),
        (
            {"https://media.test/a.m3u8": "#EXTM3U\n" + "#" * 50},
            {"max_playlist_bytes": 16},
            "playlist_too_large",
        ),
        (
            {
                "https://media.test/a.m3u8": (
                    "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n#EXT-X-ENDLIST\n"
                )
            },
            {"max_segments": 1},
            "segment_limit_exceeded",
        ),
    ],
)
def test_playlist_bounds_have_distinct_kinds(tmp_path, monkeypatch, documents, kwargs, kind):
    def fake_request(method, url, **request_kwargs):
        body = documents.get(url)
        if body is None:
            raise AssertionError(f"unexpected fetch: {url}")
        return _response(url, body.encode(), **{"Content-Length": str(len(body.encode()))})

    monkeypatch.setattr(hls, "safe_request", fake_request)
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls("https://media.test/a.m3u8", tmp_path, **kwargs)
    assert excinfo.value.kind == kind
    assert list(tmp_path.iterdir()) == []


def test_cancel_during_key_fetch_leaves_task_directory_empty(tmp_path, monkeypatch):
    token = CancellationToken()
    playlist_url = "https://media.test/index.m3u8"
    playlist = (
        b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://keys.test/key"\n'
        b"#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    )
    key_response = _response("https://keys.test/key", b"x" * 16)

    def cancelling_chunks(*args, **kwargs):
        token.cancel()
        yield b"x" * 16

    key_response.iter_content = cancelling_chunks

    def fake_request(method, url, **kwargs):
        if url == playlist_url:
            return _response(url, playlist)
        if url == "https://keys.test/key":
            return key_response
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(hls, "safe_request", fake_request)
    with pytest.raises(hls.HlsDownloadError, match="cancelled"):
        hls.download_hls(playlist_url, tmp_path, cancel_token=token)
    assert list(tmp_path.iterdir()) == []
