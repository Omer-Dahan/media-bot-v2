"""M11: the shared bar/percent/size/speed/ETA renderer used by both the
download (yt-dlp hook) and upload (byte-count) progress messages."""

import re

from media_bot_v2.telegram.progress_format import (
    BAR_EMPTY,
    BAR_FILLED,
    BAR_WIDTH,
    LRM,
    format_progress,
    human_eta,
    render_bar,
)

PHASE = "⬇️ מוריד..."


def _percent(text: str) -> int:
    match = re.search(r"(\d+)%", text)
    assert match, f"no percent found in {text!r}"
    return int(match.group(1))


# --- render_bar -------------------------------------------------------------


def test_render_bar_zero_is_all_empty():
    bar = render_bar(0)
    assert bar == BAR_EMPTY * BAR_WIDTH
    assert BAR_FILLED not in bar


def test_render_bar_hundred_is_all_filled():
    bar = render_bar(100)
    assert bar == BAR_FILLED * BAR_WIDTH
    assert BAR_EMPTY not in bar


def test_render_bar_clamps_out_of_range_input():
    assert render_bar(150) == BAR_FILLED * BAR_WIDTH
    assert render_bar(-10) == BAR_EMPTY * BAR_WIDTH


def test_render_bar_partial_fill_matches_percent():
    bar = render_bar(50, width=10)
    assert bar.count(BAR_FILLED) == 5
    assert bar.count(BAR_EMPTY) == 5


# --- human_eta ---------------------------------------------------------------


def test_human_eta_none_or_zero_is_omitted():
    assert human_eta(None) is None
    assert human_eta(0) is None


def test_human_eta_seconds_minutes_hours():
    assert human_eta(5) == "5 שניות"
    assert human_eta(125) == "2:05 דקות"
    assert human_eta(7325) == "2:02 שעות"


# --- format_progress: the four required example states ----------------------


def test_format_progress_at_zero_percent():
    text = format_progress(PHASE, transferred=0, total=100 * 1024 * 1024)
    assert text.startswith(PHASE)
    assert _percent(text) == 0
    assert BAR_EMPTY * BAR_WIDTH in text
    assert "0B" in text  # real, accurate "0 transferred" - not a placeholder
    assert "None" not in text


def test_format_progress_mid_with_speed_and_eta():
    text = format_progress(
        PHASE,
        transferred=45 * 1024 * 1024,
        total=100 * 1024 * 1024,
        speed=1.5 * 1024 * 1024,
        eta=36,
    )
    assert _percent(text) == 45
    assert "45.0MB" in text and "100.0MB" in text
    assert "מהירות" in text and "1.5MB/s" in text
    assert "זמן משוער" in text and "36 שניות" in text
    assert "None" not in text
    # bar + percent + size line is on its own line (not glued into Hebrew prose)
    lines = text.split("\n")
    assert any(BAR_FILLED in line and "45%" in line for line in lines)


def test_format_progress_unknown_total_has_no_broken_bar():
    text = format_progress(PHASE, transferred=12 * 1024 * 1024, total=None)
    assert text.startswith(PHASE)
    assert BAR_FILLED not in text and BAR_EMPTY not in text  # no misleading bar
    assert "%" not in text  # no percent without a real total to divide by
    assert "12.0MB" in text
    assert "לא ידוע" in text
    assert "None" not in text


def test_format_progress_unknown_total_still_shows_speed_and_eta_when_known():
    """Each missing piece (total, speed, ETA) is independently omittable -
    losing the total must not also hide a speed/ETA that IS known."""
    text = format_progress(PHASE, transferred=12 * 1024 * 1024, total=None, speed=1024 * 1024, eta=10)
    assert "מהירות" in text
    assert "זמן משוער" in text


def test_format_progress_at_hundred_percent():
    text = format_progress(PHASE, transferred=100 * 1024 * 1024, total=100 * 1024 * 1024)
    assert _percent(text) == 100
    assert BAR_FILLED * BAR_WIDTH in text
    assert BAR_EMPTY not in text.split("\n")[1]  # the bar line is fully filled, no leftover empty cells


def test_format_progress_never_exceeds_hundred_percent():
    """A retried/duplicated byte count must not push the shown percent past 100."""
    text = format_progress(PHASE, transferred=110 * 1024 * 1024, total=100 * 1024 * 1024)
    assert _percent(text) == 100


def test_format_progress_omits_speed_when_not_given():
    text = format_progress(PHASE, transferred=50, total=100)
    assert "מהירות" not in text
    assert "⚡" not in text


def test_format_progress_omits_eta_when_not_given():
    text = format_progress(PHASE, transferred=50, total=100)
    assert "זמן משוער" not in text
    assert "⏱️" not in text


def test_format_progress_omits_size_block_when_no_data_at_all():
    text = format_progress(PHASE)
    assert text == PHASE


# --- RTL: the bar/percent/size and speed/ETA lines carry an LTR override ----


def test_bar_line_is_prefixed_with_left_to_right_mark():
    text = format_progress(PHASE, transferred=45, total=100)
    bar_line = text.split("\n")[1]
    assert bar_line.startswith(LRM)
    # the logical (unicode codepoint) order is bar, then percent, then sizes -
    # exactly the order a LTR-reading client renders it in, once anchored by LRM
    assert bar_line.index(BAR_FILLED) < bar_line.index("%") < bar_line.index("(")


def test_speed_and_eta_lines_are_prefixed_with_left_to_right_mark():
    text = format_progress(PHASE, transferred=45, total=100, speed=10, eta=5)
    lines = text.split("\n")
    speed_line = next(line for line in lines if "מהירות" in line)
    eta_line = next(line for line in lines if "זמן משוער" in line)
    assert LRM in speed_line
    assert LRM in eta_line
    # LRM sits right before the numeric/unit content, after the Hebrew label
    assert speed_line.index("מהירות") < speed_line.index(LRM)
    assert eta_line.index("משוער") < eta_line.index(LRM)


def test_unknown_total_line_is_also_ltr_anchored():
    text = format_progress(PHASE, transferred=12, total=None)
    size_line = text.split("\n")[1]
    assert size_line.startswith(LRM)
