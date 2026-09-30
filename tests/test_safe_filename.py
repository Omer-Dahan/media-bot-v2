"""Unit tests for media_bot_v2.engines.safe_filename (findings A and D).

These write real files to a real filesystem (tmp_path) so a name that is
still too long would surface as a genuine `OSError: [Errno 36] File name
too long` on Linux, not just a string-length assertion.
"""

from __future__ import annotations

from media_bot_v2.engines.safe_filename import (
    DEFAULT_MAX_NAME_BYTES,
    build_safe_named_file,
    safe_basename_from_url_path,
    sanitize_component,
    truncate_utf8_bytes,
    within_directory,
)

_HEBREW_TITLE = "כותרת ארוכה מאוד בעברית שמתארת סרטון עם הרבה מילים ותוכן " * 6
_EMOJI_TITLE = "🎉🎬🔥🚀😀🥳🎊✨🌟💥" * 15
_MIXED_TITLE = "וידאו מדהים 😀🚀🎥 with English text mixed in " * 8


# --- truncate_utf8_bytes ----------------------------------------------------


def test_truncate_utf8_bytes_never_exceeds_budget_for_hebrew():
    result = truncate_utf8_bytes(_HEBREW_TITLE, 100)
    assert len(result.encode("utf-8")) <= 100
    assert result  # something survived


def test_truncate_utf8_bytes_never_exceeds_budget_for_emoji():
    result = truncate_utf8_bytes(_EMOJI_TITLE, 100)
    assert len(result.encode("utf-8")) <= 100
    assert result


def test_truncate_utf8_bytes_never_splits_a_multibyte_char():
    # Truncating mid-character must not raise and must not leave a partial
    # (undecodable) byte sequence - re-encoding the result must round-trip.
    for budget in range(1, 30):
        result = truncate_utf8_bytes(_EMOJI_TITLE, budget)
        result.encode("utf-8").decode("utf-8")  # raises if corrupted
        assert len(result.encode("utf-8")) <= budget


def test_truncate_utf8_bytes_returns_unchanged_when_already_short():
    assert truncate_utf8_bytes("short.mp4", 200) == "short.mp4"


def test_truncate_utf8_bytes_zero_budget_returns_empty():
    assert truncate_utf8_bytes("anything", 0) == ""


# --- safe_basename_from_url_path (also finding A) ---------------------------


def test_safe_basename_truncates_long_hebrew_and_keeps_extension(tmp_path):
    name = safe_basename_from_url_path(f"/{_HEBREW_TITLE}.mp4", default_ext=".bin")
    assert len(name.encode("utf-8")) <= DEFAULT_MAX_NAME_BYTES
    assert name.endswith(".mp4")
    # Actually writing it must succeed - the real-world failure mode (errno 36).
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


def test_safe_basename_truncates_long_emoji_and_keeps_extension(tmp_path):
    name = safe_basename_from_url_path(f"/{_EMOJI_TITLE}.mp4", default_ext=".bin")
    assert len(name.encode("utf-8")) <= DEFAULT_MAX_NAME_BYTES
    assert name.endswith(".mp4")
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


def test_safe_basename_truncates_mixed_title_and_keeps_extension(tmp_path):
    name = safe_basename_from_url_path(f"/{_MIXED_TITLE}.mp4", default_ext=".bin")
    assert len(name.encode("utf-8")) <= DEFAULT_MAX_NAME_BYTES
    assert name.endswith(".mp4")
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


def test_safe_basename_empty_after_decode_falls_back(tmp_path):
    name = safe_basename_from_url_path("/%2F%2F", default_stem="download", default_ext=".bin")
    assert name
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


# --- build_safe_named_file (finding D, yt-dlp engines' rename step) --------


def test_build_safe_named_file_keeps_suffix_intact_for_hebrew(tmp_path):
    suffix = " [abc123XYZ9].mp4"
    name = build_safe_named_file(_HEBREW_TITLE, suffix=suffix)
    assert name.endswith(suffix)
    assert len(name.encode("utf-8")) <= DEFAULT_MAX_NAME_BYTES
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


def test_build_safe_named_file_keeps_suffix_intact_for_emoji(tmp_path):
    suffix = " [emoji000ID].mp4"
    name = build_safe_named_file(_EMOJI_TITLE, suffix=suffix)
    assert name.endswith(suffix)
    assert len(name.encode("utf-8")) <= DEFAULT_MAX_NAME_BYTES
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


def test_build_safe_named_file_keeps_suffix_intact_for_mixed(tmp_path):
    suffix = " [mixed0001].he.srt"  # subtitle-style tail with a language code
    name = build_safe_named_file(_MIXED_TITLE, suffix=suffix)
    assert name.endswith(suffix)
    assert len(name.encode("utf-8")) <= DEFAULT_MAX_NAME_BYTES
    (tmp_path / name).write_bytes(b"x")
    assert (tmp_path / name).exists()


def test_build_safe_named_file_never_truncates_the_suffix_itself():
    # Even with an absurdly small budget, the id/extension-bearing suffix
    # must survive whole - only the human-readable stem gets sacrificed.
    suffix = " [abc123XYZ9].mp4"
    name = build_safe_named_file(_HEBREW_TITLE, suffix=suffix, max_bytes=len(suffix.encode()) + 1)
    assert name.endswith(suffix)


# --- sanitize_component / within_directory (finding A backstop) -----------


def test_sanitize_component_strips_separators_and_traversal():
    assert "/" not in sanitize_component("a/b/../c")
    assert "\\" not in sanitize_component("a\\b")
    assert not sanitize_component("..").strip(".")


def test_within_directory_confines_path_inside_dest_dir(tmp_path):
    result = within_directory(tmp_path, "safe_name.bin", fallback="fallback.bin")
    assert result.parent == tmp_path.resolve()
    assert result.name == "safe_name.bin"
