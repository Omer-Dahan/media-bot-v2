"""Pure-helper tests for the page-scan inline picker (no network)."""

from media_bot_v2.engines.page_scan import MediaCandidate
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.page_menu import build_markup, button_label, parse_selection


def _candidate(**overrides) -> MediaCandidate:
    defaults = {
        "url": "https://example.com/video.mp4",
        "kind": "video",
        "ext": ".mp4",
        "height": None,
        "origin": "media_tag",
        "title": None,
    }
    defaults.update(overrides)
    return MediaCandidate(**defaults)


def test_button_label_with_height():
    candidate = _candidate(url="https://example.com/a/clip.mp4", height=1080)
    label = button_label(1, candidate)
    assert label == f"1. {texts.PAGE_SCAN_KIND_VIDEO} 1080p · clip.mp4"


def test_button_label_without_height():
    candidate = _candidate(url="https://example.com/a/clip.mp4", height=None)
    label = button_label(2, candidate)
    assert label == f"2. {texts.PAGE_SCAN_KIND_VIDEO} · clip.mp4"


def test_button_label_audio_kind():
    candidate = _candidate(url="https://example.com/song.mp3", kind="audio", ext=".mp3")
    label = button_label(1, candidate)
    assert label.startswith(f"1. {texts.PAGE_SCAN_KIND_AUDIO}")


def test_button_label_hls_kind():
    candidate = _candidate(url="https://example.com/stream.m3u8", kind="hls", ext=".m3u8")
    label = button_label(1, candidate)
    assert label.startswith(f"1. {texts.PAGE_SCAN_KIND_HLS}")


def test_button_label_truncates_long_filename():
    long_name = "a" * 80 + ".mp4"
    candidate = _candidate(url=f"https://example.com/{long_name}")
    label = button_label(1, candidate)
    filename_part = label.split("· ", 1)[1]
    assert len(filename_part) <= 48
    assert filename_part.endswith("…")


def test_button_label_falls_back_to_host_without_path_segment():
    candidate = _candidate(url="https://example.com")
    label = button_label(1, candidate)
    assert "example.com" in label


def test_build_markup_round_trips_through_parse_selection():
    candidates = [_candidate(url=f"https://example.com/{i}.mp4") for i in range(1, 4)]
    markup = build_markup(candidates)

    assert len(markup) == 3
    for row in markup:
        assert len(row) == 1

    for index, row in enumerate(markup, start=1):
        button = row[0]
        parsed = parse_selection(button.type.data)
        assert parsed == index


def test_parse_selection_rejects_malformed_input():
    assert parse_selection(b"not-ps-data") is None
    assert parse_selection(b"ps") is None
    assert parse_selection(b"ps:abc") is None
    assert parse_selection(b"other:1") is None
    assert parse_selection(b"\xff\xfe") is None
