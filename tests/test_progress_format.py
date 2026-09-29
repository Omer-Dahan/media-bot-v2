"""M11: the shared bar/percent/size/speed/ETA renderer used by both the
download (yt-dlp hook) and upload (byte-count) progress messages."""

import re
import unicodedata

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


def _first_strong_is_rtl(line: str) -> bool:
    """Per Unicode bidi rule P2/P3, a paragraph's direction is decided by its
    first *strong* character - neutrals (emoji, punctuation) and weak types
    (digits) are skipped over. LRM's own bidi type is strong-L, so a line
    that starts with LRM (rather than a Hebrew letter) is LTR, not RTL."""
    for ch in line:
        bidi = unicodedata.bidirectional(ch)
        if bidi == "L":
            return False
        if bidi in ("R", "AL"):
            return True
    return False


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


def test_render_bar_never_full_below_hundred():
    """A rounded fill (e.g. round(9.5) == 10) would make 95%/99% look like a
    completed bar. Floored fill must leave at least one empty cell short of 100."""
    for percent in (94, 95, 96, 99, 99.9):
        bar = render_bar(percent, width=10)
        assert BAR_EMPTY in bar, f"{percent}% rendered a full bar: {bar!r}"


# --- human_eta ---------------------------------------------------------------


def test_human_eta_none_or_zero_is_omitted():
    assert human_eta(None) is None
    assert human_eta(0) is None


def test_human_eta_seconds_minutes_hours():
    assert human_eta(5) == "5 שניות"
    assert human_eta(125) == "2:05 דקות"
    assert human_eta(7325) == "2:02 שעות"


def test_human_eta_negative_or_non_finite_is_omitted():
    assert human_eta(-5) is None
    assert human_eta(float("inf")) is None
    assert human_eta(float("nan")) is None


# --- negative/zero speed must never be shown -------------------------------


def test_format_progress_negative_speed_is_omitted():
    text = format_progress(PHASE, transferred=50, total=100, speed=-500.0)
    assert "מהירות" not in text
    assert "⚡" not in text
    assert "-500" not in text


def test_format_progress_zero_or_non_finite_speed_is_omitted():
    for bad_speed in (0.0, float("nan"), float("inf")):
        text = format_progress(PHASE, transferred=50, total=100, speed=bad_speed)
        assert "מהירות" not in text


def test_format_progress_positive_speed_is_shown():
    text = format_progress(PHASE, transferred=50, total=100, speed=1024.0)
    assert "מהירות" in text
    assert "1.0KB/s" in text


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


# --- Bar/percent sync: one source of truth, never a bar that looks "done" -
# --- before the text says 100% -----------------------------------------------

_BOUNDARY_PERCENTS = (0, 1, 5, 45.1, 49.9, 50, 95, 99, 99.9, 100)


def test_bar_and_percent_stay_in_sync_across_boundaries():
    total = 1_000_000
    for percent in _BOUNDARY_PERCENTS:
        transferred = percent / 100 * total
        text = format_progress(PHASE, transferred=transferred, total=total)
        shown_percent = _percent(text)
        assert shown_percent == int(percent), f"percent={percent}: text shows {shown_percent}%"

        bar_line = text.split("\n")[1]
        filled = bar_line.count(BAR_FILLED)
        empty = bar_line.count(BAR_EMPTY)
        assert filled + empty == BAR_WIDTH

        if shown_percent < 100:
            assert empty > 0, f"percent={percent}: bar is fully filled before 100% ({bar_line!r})"
        else:
            assert filled == BAR_WIDTH and empty == 0


# --- RTL: the bar/percent/size and speed/ETA lines carry an LTR override ----


def test_bar_line_opens_with_hebrew_label_then_left_to_right_mark():
    text = format_progress(PHASE, transferred=45, total=100)
    bar_line = text.split("\n")[1]
    assert not bar_line.startswith(LRM)  # LRM is itself strong-L; can't be first
    assert LRM in bar_line
    # Hebrew label precedes the LRM-anchored numeric/bar run
    assert bar_line.index("התקדמות") < bar_line.index(LRM)
    # within that LTR-anchored run, logical order is bar, then percent, then sizes
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


def test_unknown_total_line_opens_with_hebrew_label_then_left_to_right_mark():
    text = format_progress(PHASE, transferred=12, total=None)
    size_line = text.split("\n")[1]
    assert not size_line.startswith(LRM)
    assert size_line.index("הורד") < size_line.index(LRM)


def test_every_line_of_a_full_message_opens_with_a_strong_rtl_character():
    """The concrete failure mode this guards against: a plain-text bidi
    renderer (or a Telegram client that computes per-line paragraph
    direction) picks LTR for any line whose first *strong* character isn't
    Hebrew/Arabic - even if LRM is present later in the line. This must hold
    for every line the formatter can produce, not just the ones with a bar."""
    text = format_progress(
        PHASE,
        transferred=45 * 1024 * 1024,
        total=100 * 1024 * 1024,
        speed=1.5 * 1024 * 1024,
        eta=36,
    )
    for line in text.split("\n"):
        assert _first_strong_is_rtl(line), f"line does not open with a strong RTL character: {line!r}"

    unknown_total_text = format_progress(PHASE, transferred=12 * 1024 * 1024, total=None)
    for line in unknown_total_text.split("\n"):
        assert _first_strong_is_rtl(line), f"line does not open with a strong RTL character: {line!r}"

    # NOTE: a string-level check like this confirms the *bidi type* of each
    # line's leading character, which decides paragraph direction. It cannot
    # confirm how Telegram's actual clients (desktop/web/Android/iOS) render
    # the full multi-line message - that still requires an eyeball check in
    # each client against a real in-progress download/upload message.
