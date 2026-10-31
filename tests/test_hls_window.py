import http.server
import shutil
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pytest
import requests

from media_bot_v2.engines import hls
from media_bot_v2.engines.base import CancellationToken, DownloadTooLargeError
from media_bot_v2.telegram import texts

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="ffmpeg/ffprobe not installed")


@pytest.fixture(scope="module")
def short_segments(tmp_path_factory):
    work = tmp_path_factory.mktemp("hls-live")
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x120:rate=10:duration=22",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=22",
            "-c:v",
            "libx264",
            "-g",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "segment",
            "-segment_time",
            "0.2",
            "-reset_timestamps",
            "1",
            str(work / "segment-%03d.ts"),
            "-loglevel",
            "error",
        ],
        check=True,
    )
    return [path.read_bytes() for path in sorted(work.glob("segment-*.ts"))]


@dataclass
class LiveState:
    segments: list[bytes]
    first: int = 1000
    count: int = 105
    refresh_advance: int = 0
    missing: set[int] = field(default_factory=set)
    discontinuity_at: int | None = None
    playlist_requests: int = 0
    get_order: list[int] = field(default_factory=list)
    head_order: list[int] = field(default_factory=list)
    cancel_token: CancellationToken | None = None
    segment_delay: float = 0.0
    active_requests: int = 0
    max_active_requests: int = 0
    request_lock: threading.Lock = field(default_factory=threading.Lock)

    def segment_bytes(self, sequence: int) -> bytes:
        return self.segments[(sequence - self.first) % len(self.segments)]

    def playlist(self) -> bytes:
        advance = self.refresh_advance if self.playlist_requests else 0
        first = self.first + advance
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for sequence in range(first, first + self.count):
            if sequence == self.discontinuity_at:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.extend(("#EXTINF:0.200000,", f"seg-{sequence}.ts"))
        self.playlist_requests += 1
        return ("\n".join(lines) + "\n").encode()


@pytest.fixture
def live_server(short_segments):
    servers = []

    def start(**overrides):
        state = LiveState(short_segments, **overrides)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/live.m3u8":
                    self._send(state.playlist(), "application/vnd.apple.mpegurl")
                    return
                sequence = int(self.path.removeprefix("/seg-").removesuffix(".ts"))
                state.get_order.append(sequence)
                if state.cancel_token is not None and len(state.get_order) == 2:
                    state.cancel_token.cancel()
                if sequence in state.missing:
                    self.send_response(404)
                    self.end_headers()
                    return
                with state.request_lock:
                    state.active_requests += 1
                    state.max_active_requests = max(
                        state.max_active_requests, state.active_requests
                    )
                try:
                    time.sleep(state.segment_delay)
                    self._send(state.segment_bytes(sequence), "video/mp2t")
                finally:
                    with state.request_lock:
                        state.active_requests -= 1

            def do_HEAD(self):
                sequence = int(self.path.removeprefix("/seg-").removesuffix(".ts"))
                state.head_order.append(sequence)
                self.send_response(200)
                self.send_header("Content-Length", str(len(state.segment_bytes(sequence))))
                self.end_headers()

            def _send(self, body, content_type):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/live.m3u8", state

    yield start
    for server, thread in servers:
        server.shutdown()
        thread.join()


def _duration(path):
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def test_window_download_has_no_duplicates(live_server, tmp_path, bypass_ssrf_guard):
    url, state = live_server()
    result = hls.download_hls_window(url, tmp_path, None, None, segment_workers=8)

    assert len(state.get_order) == len(set(state.get_order)) == 105
    assert result.window.gap_count == 0
    assert 19 < _duration(result.file_paths[0]) < 23


def test_window_segments_start_in_parallel(live_server, tmp_path, bypass_ssrf_guard):
    url, state = live_server(count=24, segment_delay=0.2)

    hls.download_hls_window(url, tmp_path, None, None, segment_workers=8)

    # Deterministic proof of concurrency: if segment starts were serialized,
    # at most one request would ever be in flight at a time.
    assert state.max_active_requests >= 8
    assert len(state.get_order) == len(set(state.get_order)) == 24


def test_gaps_succeed_under_ratio_and_fail_over_it(live_server, tmp_path, bypass_ssrf_guard):
    url, _ = live_server(count=100, missing={1000})
    result = hls.download_hls_window(url, tmp_path / "ok", None, None, max_gap_ratio=0.02)
    assert result.window.gap_count == 1
    assert result.window.missing_duration == pytest.approx(0.2)

    url, _ = live_server(count=100, missing={1000, 1001, 1002})
    failed_dir = tmp_path / "failed"
    with pytest.raises(hls.HlsUnsupportedError, match="window_expired") as exc:
        hls.download_hls_window(url, failed_dir, None, None, max_gap_ratio=0.02)
    assert exc.value.kind == "window_expired"
    assert list(failed_dir.iterdir()) == []


def test_tail_refresh_fetches_new_sequences_once(live_server, tmp_path, bypass_ssrf_guard):
    url, state = live_server(refresh_advance=3)
    hls.download_hls_window(url, tmp_path, None, None)
    assert state.playlist_requests == 2
    assert sorted(state.get_order[-3:]) == [1105, 1106, 1107]
    assert len(state.get_order) == len(set(state.get_order))


def test_window_seconds_selects_tail(live_server, tmp_path, bypass_ssrf_guard):
    url, state = live_server()
    result = hls.download_hls_window(url, tmp_path, None, None, window_seconds=2.0)
    assert len(state.get_order) == 10
    assert result.window.selected_duration == pytest.approx(2.0)
    assert result.window.delivered_duration == pytest.approx(2.0)


def test_discontinuity_remuxes_runs_and_concatenates(
    live_server, tmp_path, bypass_ssrf_guard, monkeypatch
):
    url, _ = live_server(count=20, discontinuity_at=1010)
    calls = []
    original = hls._run_ffmpeg_remux

    def recording_remux(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(hls, "_run_ffmpeg_remux", recording_remux)
    result = hls.download_hls_window(url, tmp_path, None, None)
    assert len(calls) == 2
    assert 3.5 < _duration(result.file_paths[0]) < 4.5


def test_size_cap_and_cancellation_leave_no_files(live_server, tmp_path, bypass_ssrf_guard):
    url, _ = live_server(count=10)
    size_dir = tmp_path / "size"
    with pytest.raises(DownloadTooLargeError):
        hls.download_hls_window(url, size_dir, 100, None)
    assert list(size_dir.iterdir()) == []

    token = CancellationToken()
    url, _ = live_server(count=10, cancel_token=token)
    cancel_dir = tmp_path / "cancel"
    with pytest.raises(hls.HlsDownloadError, match="cancelled"):
        hls.download_hls_window(url, cancel_dir, None, token)
    assert list(cancel_dir.iterdir()) == []


def test_analyze_window_samples_and_marks_estimate(live_server, bypass_ssrf_guard):
    url, state = live_server()
    info = hls.analyze_hls_window(url, sample_requests=6)
    assert info.size_is_estimate
    sampled_lengths = [len(state.segment_bytes(sequence)) for sequence in state.head_order]
    assert info.estimated_bytes == int(statistics.median(sampled_lengths) * state.count)
    assert info.estimated_bytes > 0
    assert state.head_order[0] == state.first
    assert state.head_order[-1] == state.first + state.count - 1
    assert len(state.head_order) == 6


def test_segments_are_requested_oldest_first_with_no_duplicates(
    live_server, tmp_path, bypass_ssrf_guard
):
    url, state = live_server(count=20, segment_delay=0.05)
    hls.download_hls_window(url, tmp_path, None, None, segment_workers=4)

    assert len(state.get_order) == len(set(state.get_order)) == 20
    oldest_four = set(range(state.first, state.first + 4))
    assert set(state.get_order[:4]) == oldest_four


def _response(url: str, body: bytes, status: int = 200, **headers: str) -> requests.Response:
    response = requests.Response()
    response.url = url
    response.status_code = status
    response._content = body
    response._content_consumed = True
    response.headers.update(headers)
    response.close = lambda: None
    return response


def test_window_http_key_is_refused_before_any_segment_request(tmp_path, monkeypatch):
    playlist_url = "https://media.test/live.m3u8"
    playlist = (
        b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1000\n"
        b'#EXT-X-KEY:METHOD=AES-128,URI="http://keys.test/key"\n'
        b"#EXTINF:1,\nseg-1000.ts\n"
    )
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        if url == playlist_url:
            return _response(url, playlist)
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(hls, "safe_request", fake_request)
    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls_window(playlist_url, tmp_path, None, None)

    assert excinfo.value.kind == "https_key_required"
    assert calls == [playlist_url]
    assert list(tmp_path.iterdir()) == []


def test_fetch_window_segment_over_cap_leaves_no_partial_or_final_file(tmp_path, monkeypatch):
    def fake_request(method, url, **kwargs):
        return _response(url, b"x" * 50)

    monkeypatch.setattr(hls, "safe_request", fake_request)
    dest = tmp_path / "seg.ts"
    counter = hls._ByteCounter(max_size=10)
    segment = hls.Segment(
        uri="seg.ts", duration=1.0, byterange=None, key=None, discontinuity=False
    )

    with pytest.raises(DownloadTooLargeError):
        hls._fetch_window_segment(segment, "https://media.test/", dest, counter, None)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_window_download_every_segment_and_key_request_goes_through_safe_request(
    tmp_path, monkeypatch
):
    """Guard coverage for the window path, checked without the
    bypass_ssrf_guard fixture: the address check runs for real (patched
    locally, not via that fixture), and every URL the download touches -
    every segment and the key - must have gone through hls.safe_request,
    the single chokepoint the SSRF guard wraps."""
    from media_bot_v2.engines import ssrf_guard

    monkeypatch.setattr(ssrf_guard, "assert_safe_url", lambda url: None)

    key_bytes = bytes(range(16))
    segments = {f"seg-{i}.ts": f"segment-{i}-body".encode() for i in range(4)}
    playlist = (
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1000\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://keys.test/window-key"\n'
        + "".join(f"#EXTINF:1.0,\nseg-{i}.ts\n" for i in range(4))
    ).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/live.m3u8":
                self._send(playlist, "application/vnd.apple.mpegurl")
                return
            body = segments.get(self.path.lstrip("/"))
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self._send(body, "video/mp2t")

        def _send(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    recorded: list[str] = []
    original = hls.safe_request

    def recording(method, u, **kwargs):
        recorded.append(u)
        if u == "https://keys.test/window-key":
            return _response(u, key_bytes)
        return original(method, u, **kwargs)

    monkeypatch.setattr(hls, "safe_request", recording)

    try:
        with pytest.raises(hls.HlsDownloadError):
            # The fake segment bodies aren't valid MPEG-TS, so the ffmpeg
            # remux step fails - that's fine, this test only verifies fetch
            # coverage, which happens before the remux.
            hls.download_hls_window(
                f"http://127.0.0.1:{server.server_port}/live.m3u8", tmp_path, None, None
            )
    finally:
        server.shutdown()
        thread.join()

    expected_segments = {f"http://127.0.0.1:{server.server_port}/seg-{i}.ts" for i in range(4)}
    assert expected_segments <= set(recorded)
    assert "https://keys.test/window-key" in recorded


def test_tail_refresh_appends_without_dropping_snapshot(live_server, tmp_path, bypass_ssrf_guard):
    url, state = live_server(refresh_advance=5)
    result = hls.download_hls_window(url, tmp_path, None, None)

    # Snapshot window (105 segments) plus the appended tail (5 more
    # published during the download) - never a re-selected, shifted window
    # that would have dropped the snapshot's oldest segments.
    assert result.window.selected_duration == pytest.approx(110 * 0.2)
    assert result.window.delivered_duration == pytest.approx(110 * 0.2)
    assert result.window.gap_count == 0
    assert set(range(1000, 1110)) <= set(state.get_order)
    assert len(set(state.get_order)) == 110


def test_gaps_under_all_limits_succeed_and_message_mentions_them(live_server, tmp_path, bypass_ssrf_guard):
    url, _ = live_server(count=250, missing={1000, 1050})
    result = hls.download_hls_window(url, tmp_path, None, None, max_gap_ratio=0.02)

    assert result.window.gap_count == 2
    assert result.window.missing_duration == pytest.approx(0.4)

    message = texts.format_download_summary(
        window_gap_count=result.window.gap_count,
        window_delivered_duration=result.window.delivered_duration,
        window_requested_duration=result.window.selected_duration,
    )
    assert "2" in message
    assert "מקטעים חסרים" in message


def test_gap_count_limit_triggers_window_expired_independent_of_ratio(
    live_server, tmp_path, bypass_ssrf_guard
):
    # 4 gaps out of 250 segments is only a 1.6% ratio - well under the 2%
    # ratio cap - but the absolute count cap (3) must still catch it.
    url, _ = live_server(count=250, missing={1000, 1050, 1100, 1150})
    with pytest.raises(hls.HlsUnsupportedError, match="window_expired") as exc:
        hls.download_hls_window(url, tmp_path, None, None, max_gap_ratio=0.02)
    assert exc.value.kind == "window_expired"
    assert list(tmp_path.iterdir()) == []


def test_single_large_missing_segment_triggers_duration_limit(tmp_path, monkeypatch):
    """1199 normal 1s segments plus one anomalous missing 3600s segment:
    gap_count=1 (under the abs cap of 3) and gap_ratio=1/1200 (well under
    the 2% ratio cap), yet the missing segment alone is most of the
    delivered content - only the duration-based cap catches this."""
    playlist_url = "https://media.test/live.m3u8"
    lines = ["#EXTM3U", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(1199):
        lines += ["#EXTINF:1.000000,", f"seg-{i}.ts"]
    lines += ["#EXTINF:3600.000000,", "seg-1199.ts"]
    playlist_body = ("\n".join(lines) + "\n").encode()

    def fake_request(method, url, **kwargs):
        if url == playlist_url:
            return _response(url, playlist_body)
        if url.endswith("seg-1199.ts"):
            return _response(url, b"", status=404)
        return _response(url, b"x")

    monkeypatch.setattr(hls, "safe_request", fake_request)

    with pytest.raises(hls.HlsUnsupportedError, match="window_expired") as exc:
        hls.download_hls_window(playlist_url, tmp_path, None, None)
    assert exc.value.kind == "window_expired"
    assert list(tmp_path.iterdir()) == []


def test_audio_rendition_404_raises_classified_error(tmp_path, monkeypatch):
    master_url = "https://media.test/master.m3u8"
    video_url = "https://media.test/video.m3u8"
    audio_url = "https://media.test/audio.m3u8"
    master_body = (
        b"#EXTM3U\n"
        b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="main",DEFAULT=YES,URI="audio.m3u8"\n'
        b'#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360,AUDIO="aud"\n'
        b"video.m3u8\n"
    )
    video_body = (
        b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1000\n"
        b"#EXTINF:1.0,\nseg-1000.ts\n#EXTINF:1.0,\nseg-1001.ts\n"
    )

    def fake_request(method, url, **kwargs):
        if url == master_url:
            return _response(url, master_body)
        if url == video_url:
            return _response(url, video_body)
        if url == audio_url:
            return _response(url, b"", status=404)
        if url.endswith(".ts"):
            return _response(url, b"fake-ts-bytes")
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(hls, "safe_request", fake_request)

    with pytest.raises(hls.HlsUnsupportedError) as excinfo:
        hls.download_hls_window(master_url, tmp_path, None, None)

    assert excinfo.value.kind == "audio_rendition_unavailable"
    assert list(tmp_path.iterdir()) == []


def test_segment_batch_failure_cancels_remaining_futures(tmp_path, monkeypatch):
    shutdown_calls: list[bool] = []
    original_shutdown = ThreadPoolExecutor.shutdown

    def spy_shutdown(self, wait=True, cancel_futures=False):
        shutdown_calls.append(cancel_futures)
        return original_shutdown(self, wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", spy_shutdown)

    def fake_fetch(segment, base_url, dest, counter, cancel_token):
        if segment.sequence == 0:
            raise hls.HlsDownloadError("boom")
        return hls._DownloadedSegment(segment, dest)

    monkeypatch.setattr(hls, "_fetch_window_segment", fake_fetch)
    segments = [
        hls.Segment(uri=f"seg{i}.ts", duration=1.0, byterange=None, key=None, discontinuity=False, sequence=i)
        for i in range(5)
    ]
    existing: dict[int, hls._DownloadedSegment] = {}
    with pytest.raises(hls.HlsDownloadError):
        hls._download_segment_batch(
            segments, "https://media.test/", tmp_path, hls._ByteCounter(None), None, 1, existing
        )

    assert True in shutdown_calls


def test_cancel_after_probe_leaves_no_final_file(live_server, tmp_path, bypass_ssrf_guard, monkeypatch):
    url, _ = live_server(count=20)
    token = CancellationToken()
    original_probe = hls._probe_duration

    def probe_then_cancel(path):
        duration = original_probe(path)
        token.cancel()
        return duration

    monkeypatch.setattr(hls, "_probe_duration", probe_then_cancel)

    with pytest.raises(hls.HlsDownloadError, match="cancelled"):
        hls.download_hls_window(url, tmp_path, None, token)

    assert not (tmp_path / "hls-download.mp4").exists()
    assert list(tmp_path.iterdir()) == []
