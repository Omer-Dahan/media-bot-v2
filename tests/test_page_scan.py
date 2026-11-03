from __future__ import annotations

import pytest

from media_bot_v2.engines import page_scan
from media_bot_v2.engines.page_scan import MediaCandidate, scan_page
from media_bot_v2.engines.ssrf_guard import SSRFBlockedError

PAGE_URL = "https://example.com/watch"


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.status_code = 200
        self.closed = False

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True


def _patch_response(monkeypatch, html: str, content_type: str = "text/html; charset=utf-8"):
    response = _FakeResponse(html.encode("utf-8"), content_type)
    monkeypatch.setattr(page_scan, "safe_request", lambda *a, **k: response)
    return response


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """The sandbox has no network access, so DNS resolution of any test
    domain fails; stub assert_safe_url to allow http(s) URLs through so
    candidate validation can be tested without real DNS."""

    def _fake_assert(url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise SSRFBlockedError("bad scheme")

    monkeypatch.setattr(page_scan, "assert_safe_url", _fake_assert)


def test_og_video_and_twitter_player(monkeypatch):
    html = """
    <html><head>
    <meta property="og:video" content="https://cdn.example.com/a.mp4">
    <meta name="twitter:player:stream" content="https://cdn.example.com/b.mp4">
    </head><body></body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    urls = {c.url for c in candidates}
    assert "https://cdn.example.com/a.mp4" in urls
    assert "https://cdn.example.com/b.mp4" in urls
    assert all(c.origin == "og" for c in candidates)


def test_video_src_and_source_src(monkeypatch):
    html = """
    <html><body>
    <video src="https://cdn.example.com/v1.mp4"></video>
    <video><source src="https://cdn.example.com/v2.webm"></video>
    <audio><source src="https://cdn.example.com/a1.mp3"></audio>
    </body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    by_url = {c.url: c for c in candidates}
    assert by_url["https://cdn.example.com/v1.mp4"].kind == "video"
    assert by_url["https://cdn.example.com/v2.webm"].kind == "video"
    assert by_url["https://cdn.example.com/a1.mp3"].kind == "audio"
    assert all(c.origin == "media_tag" for c in candidates)


def test_json_ld_content_url(monkeypatch):
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type": "VideoObject", "contentUrl": "https://cdn.example.com/ld.mp4", "height": 720}
    </script>
    </head></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    assert len(candidates) == 1
    assert candidates[0].url == "https://cdn.example.com/ld.mp4"
    assert candidates[0].origin == "json_ld"
    assert candidates[0].height == 720


def test_inline_json_escapes(monkeypatch):
    html = (
        '<html><body><script>var x = {"url": "https:\\/\\/cdn.example.com\\/clip.mp4'
        '?a=1&amp;b=2",};</script></body></html>'
    )
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    assert len(candidates) == 1
    assert candidates[0].url == "https://cdn.example.com/clip.mp4?a=1&b=2"
    assert candidates[0].origin == "inline_json"


def test_relative_url_resolved_against_page_url(monkeypatch):
    html = """
    <html><body><video src="/videos/rel.mp4"></video></body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    assert candidates[0].url == "https://example.com/videos/rel.mp4"


def test_relative_url_resolved_against_base_href(monkeypatch):
    html = """
    <html><head><base href="https://cdn.example.com/media/"></head>
    <body><video src="rel2.mp4"></video></body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    assert candidates[0].url == "https://cdn.example.com/media/rel2.mp4"


def test_dedupe_same_url_found_twice(monkeypatch):
    html = """
    <html><head>
    <meta property="og:video" content="https://cdn.example.com/dup.mp4">
    </head><body>
    <video src="https://cdn.example.com/dup.mp4"></video>
    </body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    assert len(candidates) == 1
    assert candidates[0].origin == "og"


def test_blob_and_data_and_mpd_dropped(monkeypatch):
    html = """
    <html><body>
    <video src="blob:https://example.com/abcd"></video>
    <video src="data:video/mp4;base64,AAAA"></video>
    <video src="https://cdn.example.com/manifest.mpd"></video>
    <video src="https://cdn.example.com/keep.mp4"></video>
    </body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    assert len(candidates) == 1
    assert candidates[0].url == "https://cdn.example.com/keep.mp4"


def test_max_bytes_truncation_does_not_raise(monkeypatch):
    filler = "x" * 5000
    html = f"<html><body><!-- {filler} --><video src=\"https://cdn.example.com/a.mp4\"></video></body></html>"
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL, max_bytes=100)
    assert isinstance(candidates, list)


def test_max_candidates_respected(monkeypatch):
    metas = "".join(
        f'<meta property="og:video" content="https://cdn.example.com/v{i}.mp4">' for i in range(30)
    )
    html = f"<html><head>{metas}</head></html>"
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL, max_candidates=5)
    assert len(candidates) == 5


def test_non_html_content_type_returns_empty(monkeypatch):
    _patch_response(monkeypatch, '{"not": "html"}', content_type="application/json")
    assert scan_page(PAGE_URL) == []


def test_ssrf_blocked_error_propagates(monkeypatch):
    def _raise(*args, **kwargs):
        raise SSRFBlockedError("blocked")

    monkeypatch.setattr(page_scan, "safe_request", _raise)
    with pytest.raises(SSRFBlockedError):
        scan_page(PAGE_URL)


def test_assert_safe_url_rejection_removes_candidate_keeps_others(monkeypatch):
    html = """
    <html><body>
    <video src="https://blocked.example.com/bad.mp4"></video>
    <video src="https://cdn.example.com/good.mp4"></video>
    </body></html>
    """
    _patch_response(monkeypatch, html)

    def _fake_assert(url: str) -> None:
        if "blocked.example.com" in url:
            raise SSRFBlockedError("blocked")

    monkeypatch.setattr(page_scan, "assert_safe_url", _fake_assert)
    candidates = scan_page(PAGE_URL)
    assert len(candidates) == 1
    assert candidates[0].url == "https://cdn.example.com/good.mp4"


def test_sort_order_video_before_audio_before_hls_height_desc(monkeypatch):
    html = """
    <html><body>
    <video src="https://cdn.example.com/video_480p.mp4"></video>
    <video src="https://cdn.example.com/video_1080p.mp4"></video>
    <audio src="https://cdn.example.com/track.mp3"></audio>
    <video src="https://cdn.example.com/stream.m3u8"></video>
    </body></html>
    """
    _patch_response(monkeypatch, html)
    candidates = scan_page(PAGE_URL)
    kinds = [c.kind for c in candidates]
    assert kinds == ["video", "video", "audio", "hls"]
    assert candidates[0].height == 1080
    assert candidates[1].height == 480


def test_describe_candidate_is_ascii_label():
    candidate = MediaCandidate(
        url="https://cdn.example.com/a.mp4",
        kind="video",
        ext=".mp4",
        height=1080,
        origin="og",
    )
    label = page_scan.describe_candidate(candidate)
    assert "1080p" in label
    assert "a.mp4" in label
