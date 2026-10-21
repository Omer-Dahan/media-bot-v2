"""Regression tests for the upload progress header showing the item's real
title instead of the generic "מעלה לטלגרם..." phase name, and part labels
for split files (see pipeline._upload_label_for, progress.UploadProgress.set_label,
progress._is_terminal_text)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from media_bot_v2.engines.base import DownloadResult
from media_bot_v2.pipeline import _FileGroup, _upload_label_for
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress import UploadProgress, _is_terminal_text
from media_bot_v2.upload.media_probe import MediaInfo


def _group(title: str | None, parts: list[str]) -> _FileGroup:
    from pathlib import Path

    return _FileGroup(info=MediaInfo(title=title), parts=[Path(p) for p in parts])


def test_upload_label_uses_title_for_single_part():
    group = _group("My Cool Video", ["my_cool_video.mp4"])
    result = DownloadResult(file_paths=["my_cool_video.mp4"], title="fallback title")

    label = _upload_label_for(group, result, "")

    assert label == "מעלה: My Cool Video"
    assert label != texts.UPLOADING


def test_upload_label_includes_part_number_when_split():
    group = _group("Big File", ["big_file.mp4.part001"])
    result = DownloadResult(file_paths=["big_file.mp4"], title="Big File")

    label = _upload_label_for(group, result, "📎 חלק 2/3")

    assert "Big File" in label
    assert "2/3" in label


def test_upload_label_falls_back_to_phase_text_without_title_or_filename():
    group = _group(None, [])
    result = DownloadResult(file_paths=[], title=None)

    label = _upload_label_for(group, result, "")

    assert label == texts.UPLOADING


def test_upload_label_falls_back_to_result_title_when_no_probed_title():
    group = _group(None, ["video.mp4"])
    result = DownloadResult(file_paths=["video.mp4"], title="Result Title")

    label = _upload_label_for(group, result, "")

    assert label == "מעלה: Result Title"


def test_title_containing_failure_words_is_not_terminal():
    # A real-world title can legitimately contain the words that would
    # otherwise flag a progress line as a terminal failure/cancel report.
    text_failed = "⬆️ מעלה: הסרטון שהיא נכשלה בלהעלות"
    text_cancelled = "⬆️ מעלה: הפעולה שבוטלה בסרט"

    assert _is_terminal_text(text_failed) is False
    assert _is_terminal_text(text_cancelled) is False


@pytest.mark.asyncio
async def test_set_label_takes_effect_on_next_edit():
    reporter = MagicMock()
    reporter.update = AsyncMock()

    upload_progress = UploadProgress(reporter, texts.UPLOADING, total=1000)
    upload_progress.set_label("מעלה: Some Title (📎 חלק 1/2)")

    await upload_progress(500, 1000)

    sent_text = reporter.update.call_args[0][0]
    assert "Some Title" in sent_text
    assert sent_text.startswith("⬆️ ")
