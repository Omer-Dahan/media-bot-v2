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
from media_bot_v2.telegram import texts

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


def _probe_handler():
    """No ffmpeg/real segments needed - `probe_hls_signal` never reads past
    the first few bytes of a body or the playlist text itself."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):
            if self.path == "/mime-only-live":
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.end_headers()
            else:
                # No HEAD support for any other path - forces probe_hls_signal
                # to fall through to its GET-based check, same as a server
                # that simply doesn't implement HEAD.
                self.send_response(501)
                self.end_headers()

        def do_GET(self):
            if self.path == "/mime-only-live":
                self._text(LIVE_PLAYLIST, "application/vnd.apple.mpegurl")
            elif self.path == "/signature-only-live":
                # Generic Content-Type on both HEAD (unsupported, 501) and
                # GET - only the #EXTM3U body signature identifies this.
                self._text(LIVE_PLAYLIST, "application/octet-stream")
            elif self.path == "/ordinary-binary":
                self._text("not a playlist at all", "application/octet-stream")
            else:
                self.send_response(404)
                self.end_headers()

        def _text(self, text, content_type):
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


@pytest.fixture
def probe_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _probe_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()


def test_probe_hls_signal_detects_mime_only_live_playlist(probe_server, bypass_ssrf_guard):
    """No `.m3u8` extension - only the HLS Content-Type (seen on HEAD)
    identifies this as HLS (B2.4: a live playlist served this way must
    still reach the live-window preflight, not an unconfirmed download)."""
    assert hls.probe_hls_signal(f"{probe_server}/mime-only-live") is True


def test_probe_hls_signal_detects_signature_only_live_playlist(probe_server, bypass_ssrf_guard):
    """No extension and a generic Content-Type on both HEAD and GET - only
    the buffered `#EXTM3U` body signature identifies this as HLS."""
    assert hls.probe_hls_signal(f"{probe_server}/signature-only-live") is True


def test_probe_hls_signal_rejects_ordinary_binary(probe_server, bypass_ssrf_guard):
    assert hls.probe_hls_signal(f"{probe_server}/ordinary-binary") is False


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


def _decoded_video_checksum(path) -> str:
    """MD5 of the fully decoded video stream - distinguishes a bit-exact
    decrypt from one where only codec/duration happen to look right (e.g. a
    wrong AES IV only corrupts part of a segment under CBC)."""
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "md5", "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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

    # Codec/duration alone pass even with a wrong derived IV (CBC only
    # corrupts the first block of the affected segment); decode both
    # streams and compare bit-exactly against the original clear source.
    clear_concat = tmp_path / "clear_concat.ts"
    clear_concat.write_bytes(ts_segments[0] + ts_segments[1])
    assert _decoded_video_checksum(result.file_paths[0]) == _decoded_video_checksum(clear_concat)


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


@pytest.mark.parametrize(
    ("key_tag", "expected_kind"),
    [
        ('METHOD=SAMPLE-AES,URI="https://keys.test/key"', "sample_aes_unsupported"),
        ('METHOD=SAMPLE-AES-CTR,URI="https://keys.test/key"', "sample_aes_unsupported"),
        (
            'METHOD=AES-128,URI="https://keys.test/key",KEYFORMAT="com.apple.streamingkeydelivery"',
            "drm_unsupported",
        ),
        (
            'METHOD=AES-128,URI="https://keys.test/key",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"',
            "drm_unsupported",
        ),
        (
            'METHOD=AES-128,URI="https://keys.test/key",KEYFORMAT="com.microsoft.playready"',
            "drm_unsupported",
        ),
    ],
)
def test_sample_aes_and_drm_are_refused_before_media_or_key_fetch(
    tmp_path, monkeypatch, key_tag, expected_kind
):
    playlist_url = "https://media.test/index.m3u8"
    playlist = (
        f"#EXTM3U\n#EXT-X-KEY:{key_tag}\n"
        "#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"
    ).encode()
    requests_issued: list[str] = []

    def recording_request(method, url, **kwargs):
        requests_issued.append(url)
        if url == playlist_url:
            return _response(url, playlist)
        raise AssertionError(f"media or key request issued before refusal: {url}")

    monkeypatch.setattr(hls, "safe_request", recording_request)
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls(playlist_url, tmp_path)

    assert excinfo.value.kind == expected_kind
    assert requests_issued == [playlist_url]
    assert not any(url.endswith("segment.ts") or "keys.test" in url for url in requests_issued)


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


# ---------------------------------------------------------------------------
# Audio rendition URL resolution (must use the outer master's URL, not the
# selected video variant's own playlist URL)
# ---------------------------------------------------------------------------


def test_audio_rendition_resolves_against_master_url_not_variant_url(
    ts_segments, tmp_path, bypass_ssrf_guard
):
    """The AUDIO rendition's URI is relative to the outermost master
    playlist, not to the selected video variant's own playlist URL - the two
    live in different directories here, so resolving against the wrong base
    404s instead of reaching the real audio playlist."""
    seg0, seg1 = ts_segments
    master = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,'
        'URI="audio/en/index.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=160x120,AUDIO="aud"\n'
        "video/v/index.m3u8\n"
    )
    media_playlist = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
        "#EXTINF:1.000000,\nseg0.ts\n#EXTINF:1.000000,\nseg1.ts\n#EXT-X-ENDLIST\n"
    )
    request_log: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            request_log.append(self.path)
            routes = {
                "/master.m3u8": (master, "application/vnd.apple.mpegurl"),
                "/video/v/index.m3u8": (media_playlist, "application/vnd.apple.mpegurl"),
                "/audio/en/index.m3u8": (media_playlist, "application/vnd.apple.mpegurl"),
            }
            if self.path in routes:
                text, content_type = routes[self.path]
                self._text(text, content_type)
                return
            segments = {
                "/video/v/seg0.ts": seg0,
                "/video/v/seg1.ts": seg1,
                "/audio/en/seg0.ts": seg0,
                "/audio/en/seg1.ts": seg1,
            }
            body = segments.get(self.path)
            if body is not None:
                self._bytes(body, "video/mp2t")
                return
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

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = hls.download_hls(f"http://127.0.0.1:{server.server_port}/master.m3u8", tmp_path)
    finally:
        server.shutdown()
        thread.join()

    assert "/audio/en/index.m3u8" in request_log
    assert "/video/v/audio/en/index.m3u8" not in request_log

    probe = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            result.file_paths[0],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "audio" in probe.stdout
    assert "video" in probe.stdout


# ---------------------------------------------------------------------------
# Key transport: a redirect that downgrades https -> http must be refused
# ---------------------------------------------------------------------------


def test_https_key_redirecting_to_http_is_refused_without_reading_bytes(tmp_path, monkeypatch):
    playlist_url = "https://media.test/index.m3u8"
    playlist = (
        b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://keys.test/key"\n'
        b"#EXTINF:1,\nseg.ts\n#EXT-X-ENDLIST\n"
    )
    read_happened = False

    def exploding_iter_content(*args, **kwargs):
        nonlocal read_happened
        read_happened = True
        yield b"x" * 16

    def fake_request(method, url, **kwargs):
        if url == playlist_url:
            return _response(url, playlist)
        if url == "https://keys.test/key":
            redirected = _response("http://keys.test/key-redirected", b"")
            redirected.iter_content = exploding_iter_content
            return redirected
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(hls, "safe_request", fake_request)
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls(playlist_url, tmp_path)

    assert excinfo.value.kind == "https_key_required"
    assert not read_happened
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# ffmpeg boundary: no stdin, no network protocol, no wildcard extensions
# ---------------------------------------------------------------------------


def test_run_process_feeds_ffmpeg_with_no_stdin(monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return b"", b""

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(hls.subprocess, "Popen", fake_popen)
    hls._run_process(["ffmpeg"], 10.0, None)
    assert captured["stdin"] == hls.subprocess.DEVNULL


def test_ffmpeg_remux_argv_locks_down_protocols_and_extensions(tmp_path, monkeypatch):
    video_playlist = tmp_path / "video.m3u8"
    video_playlist.write_text("#EXTM3U\n#EXT-X-ENDLIST\n", encoding="utf-8")
    captured_cmds = []

    def fake_run_process(cmd, timeout, cancel_token):
        captured_cmds.append(cmd)

    monkeypatch.setattr(hls, "_run_process", fake_run_process)
    monkeypatch.setattr(hls, "_ffmpeg_supports_extension_picky", lambda: True)
    hls._run_ffmpeg_remux(video_playlist, None, tmp_path / "out.mp4", 10.0, None)

    cmd = captured_cmds[0]
    assert "-nostdin" in cmd
    assert "-extension_picky" in cmd
    assert cmd[cmd.index("-extension_picky") + 1] == "1"
    assert "ALL" not in cmd


def test_ffmpeg_remux_omits_extension_picky_when_unsupported(tmp_path, monkeypatch):
    video_playlist = tmp_path / "video.m3u8"
    video_playlist.write_text("#EXTM3U\n#EXT-X-ENDLIST\n", encoding="utf-8")
    captured_cmds = []

    def fake_run_process(cmd, timeout, cancel_token):
        captured_cmds.append(cmd)

    monkeypatch.setattr(hls, "_run_process", fake_run_process)
    monkeypatch.setattr(hls, "_ffmpeg_supports_extension_picky", lambda: False)
    hls._run_ffmpeg_remux(video_playlist, None, tmp_path / "out.mp4", 10.0, None)

    cmd = captured_cmds[0]
    assert "-extension_picky" not in cmd
    assert "-allowed_extensions" in cmd
    assert cmd[cmd.index("-allowed_extensions") + 1] == "ts,m4s,mp4,aac,m4a,key"


def test_extension_picky_probe_runs_at_most_once(monkeypatch):
    hls._extension_picky_supported = None
    calls = []

    class FakeResult:
        returncode = 0
        stdout = b"-extension_picky <boolean> ..."
        stderr = b""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(hls.subprocess, "run", fake_run)
    try:
        assert hls._ffmpeg_supports_extension_picky() is True
        assert hls._ffmpeg_supports_extension_picky() is True
        assert len(calls) == 1
    finally:
        hls._extension_picky_supported = None


def test_extension_picky_probe_failure_is_treated_as_unsupported(monkeypatch):
    hls._extension_picky_supported = None

    def fake_run(cmd, **kwargs):
        raise OSError("ffmpeg not found")

    monkeypatch.setattr(hls.subprocess, "run", fake_run)
    try:
        assert hls._ffmpeg_supports_extension_picky() is False
    finally:
        hls._extension_picky_supported = None


# Every kind engines/hls.py's HlsUnsupportedError actually raises (grep
# `raise HlsUnsupportedError("<kind>"`). Kept explicit, not derived from
# texts.HLS_KIND_TEXT, so this test fails loudly the day a new raise site
# invents a kind the shared classification table doesn't know about yet.
_ALL_HLS_KINDS = (
    "live",
    "malformed",
    "sample_aes_unsupported",
    "drm_unsupported",
    "https_key_required",
    "playlist_too_large",
    "segment_limit_exceeded",
    "playlist_cycle",
    "playlist_depth_exceeded",
    "window_too_short",
    "window_expired",
    "audio_rendition_unavailable",
)


def test_every_hls_kind_has_a_distinct_hebrew_text():
    """B2.2: one kind -> Hebrew text mapping, shared by the direct engine,
    the live-window engine, the preflight, and the pipeline - a hopeless
    kind must never fall back to the generic DOWNLOAD_FAILED text, which
    would hide *why* retrying is pointless."""
    assert set(_ALL_HLS_KINDS) <= set(texts.HLS_KIND_TEXT)
    for kind in _ALL_HLS_KINDS:
        text = texts.hls_failure_text(kind)
        assert text, f"no text for kind={kind}"
        assert any("֐" <= ch <= "׿" for ch in text), (
            f"text for kind={kind} is not Hebrew: {text!r}"
        )
        if not texts.hls_retry_offered(kind):
            assert text != texts.DOWNLOAD_FAILED, (
                f"hopeless kind={kind} fell back to the generic failure text"
            )


def test_hls_exception_user_text_never_leaks_the_internal_debug_message():
    """The internal debug message engines/hls.py raises an HlsUnsupportedError
    with (English, for logs) must never reach the user - only the
    classified Hebrew text for its `kind` does."""
    raw_messages = {
        "sample_aes_unsupported": "SAMPLE-AES is not supported",
        "drm_unsupported": "DRM-protected streams are not supported",
        "malformed": "missing #EXTM3U header",
    }
    for kind, debug_message in raw_messages.items():
        raw_exc = hls.HlsUnsupportedError(kind, debug_message)
        assert str(raw_exc) == debug_message  # sanity: matches what hls.py itself raises
        assert debug_message not in texts.hls_failure_text(kind)
