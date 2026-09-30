"""Unit tests for media_bot_v2.engines.ytdlp_support's finding-D helpers:
`safe_outtmpl` (id-based, always-safe download-time name) and
`rename_to_safe_display_name` (post-download rename to a human-readable,
still byte-capped name). Real files, real renames - a regression here would
surface as a genuine OSError on Linux, not just a string check.
"""

from __future__ import annotations

from pathlib import Path

from media_bot_v2.engines.ytdlp_support import rename_to_safe_display_name, safe_outtmpl

_HEBREW_TITLE = "כותרת ארוכה מאוד בעברית שמתארת סרטון עם הרבה מילים ותוכן " * 6
_EMOJI_TITLE = "🎉🎬🔥🚀😀🥳🎊✨🌟💥" * 15
_MIXED_TITLE = "וידאו מדהים 😀🚀🎥 with English text mixed in " * 8


def test_safe_outtmpl_is_id_based_and_short(tmp_path):
    tmpl = safe_outtmpl(tmp_path)
    assert tmpl == str(tmp_path / "%(id)s.%(ext)s")


def test_rename_survives_long_hebrew_title(tmp_path):
    video_id = "heb1234567"
    downloaded = tmp_path / f"{video_id}.mp4"
    downloaded.write_bytes(b"real video bytes")

    new_path_str = rename_to_safe_display_name(
        {"id": video_id, "title": _HEBREW_TITLE}, str(downloaded)
    )
    new_path = Path(new_path_str)

    assert new_path.exists()
    assert not downloaded.exists()  # renamed, not copied
    assert new_path.read_bytes() == b"real video bytes"
    assert len(new_path.name.encode("utf-8")) <= 255
    assert video_id in new_path.name
    assert new_path.suffix == ".mp4"


def test_rename_survives_long_emoji_title(tmp_path):
    video_id = "emo1234567"
    downloaded = tmp_path / f"{video_id}.mp4"
    downloaded.write_bytes(b"real video bytes")

    new_path = Path(
        rename_to_safe_display_name({"id": video_id, "title": _EMOJI_TITLE}, str(downloaded))
    )

    assert new_path.exists()
    assert len(new_path.name.encode("utf-8")) <= 255
    assert video_id in new_path.name


def test_rename_survives_long_mixed_title(tmp_path):
    video_id = "mix1234567"
    downloaded = tmp_path / f"{video_id}.mp4"
    downloaded.write_bytes(b"real video bytes")

    new_path = Path(
        rename_to_safe_display_name({"id": video_id, "title": _MIXED_TITLE}, str(downloaded))
    )

    assert new_path.exists()
    assert len(new_path.name.encode("utf-8")) <= 255
    assert video_id in new_path.name


def test_rename_preserves_subtitle_language_tail(tmp_path):
    """A subtitle file's real name is `<id>.<lang>.srt` (yt-dlp appends the
    language before the extension) - the rename must keep that tail intact,
    not just the final extension."""
    video_id = "sub1234567"
    downloaded = tmp_path / f"{video_id}.he.srt"
    downloaded.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nHello\n")

    new_path = Path(
        rename_to_safe_display_name({"id": video_id, "title": _HEBREW_TITLE}, str(downloaded))
    )

    assert new_path.exists()
    assert new_path.name.endswith(".he.srt")
    assert len(new_path.name.encode("utf-8")) <= 255


def test_rename_leaves_path_untouched_when_name_does_not_start_with_id(tmp_path):
    """Defensive fallback: an unexpected filename shape (doesn't start with
    the entry's id) must not be guessed at - the original path survives."""
    stray = tmp_path / "unexpected-name.mp4"
    stray.write_bytes(b"data")

    result = rename_to_safe_display_name({"id": "someid", "title": "Title"}, str(stray))

    assert result == str(stray)
    assert stray.exists()


def test_rename_is_idempotent_when_no_truncation_needed(tmp_path):
    video_id = "short0001"
    downloaded = tmp_path / f"{video_id}.mp4"
    downloaded.write_bytes(b"data")

    result = rename_to_safe_display_name({"id": video_id, "title": "Short Title"}, str(downloaded))

    assert Path(result).exists()
    assert video_id in Path(result).name
