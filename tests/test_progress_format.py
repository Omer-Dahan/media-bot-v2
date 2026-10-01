"""M11/M11.3/M11.19: the shared bar/percent/size/speed/ETA renderer used by
both the download (yt-dlp hook) and upload (byte-count) progress messages."""

import re
import unicodedata

from media_bot_v2.telegram.progress_format import (
    BAR_EMPTY,
    BAR_FILLED,
    BAR_WIDTH,
    LRM,
    MOON_HALF,
    MOON_QUARTER,
    MOON_THREE_QUARTER,
    format_progress,
    human_eta,
    render_bar,
)

_ALL_MOONS = {BAR_EMPTY, MOON_QUARTER, MOON_HALF, MOON_THREE_QUARTER, BAR_FILLED}

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


def _has_no_strong_character(line: str) -> bool:
    """True if `line` contains no bidi-strong (L/R/AL) character at all - per
    UAX#9 P2/P3 this is the condition under which a paragraph's direction
    falls back to the default (LTR for plain text, per P3), independent of
    any surrounding context. This is the exact property the M11.19 fix
    relies on for the bar/percent line: see progress_format.py's module and
    `render_bar` docstrings."""
    return all(unicodedata.bidirectional(ch) not in ("L", "R", "AL") for ch in line)


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
    """The last cell is permanently reserved as empty below 100% (see
    `render_bar`'s docstring), so 50% with width=10 lands on 4 full moons +
    1 half moon + 5 empty - not a clean 5/5 split."""
    bar = render_bar(50, width=10)
    assert len(bar) == 10
    assert all(ch in _ALL_MOONS for ch in bar)
    assert bar.count(BAR_FILLED) == 4
    assert bar.count(MOON_HALF) == 1
    assert bar.count(BAR_EMPTY) == 5


def test_render_bar_is_typed_empty_to_full_not_full_to_empty():
    """`render_bar`'s returned string is typed empty-cells-first,
    full-cells-last (see its docstring for the bidi reasoning): the bar line
    it's placed on (see `format_progress`) has no Hebrew label and thus no
    strong-RTL character, so it renders left-to-right in exactly this typed
    order - full moons must be last so the bar visibly "fills toward the
    right" as percent increases."""
    bar = render_bar(50, width=10)
    assert bar == BAR_EMPTY * 5 + MOON_HALF + BAR_FILLED * 4
    # every empty cell precedes every full cell (partial sits in between)
    assert bar.rindex(BAR_EMPTY) < bar.index(BAR_FILLED)


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


# --- non-numeric speed/eta must not raise (finding 0.2) ----------------------


def test_format_progress_non_numeric_speed_is_silently_omitted():
    """A yt-dlp hook can report speed as `""` (unset) rather than `None` -
    `"" > 0` raises `TypeError`, so this must not raise either."""
    for junk_speed in ("", "unknown", None, [], {}):
        text = format_progress(PHASE, transferred=50, total=100, speed=junk_speed)
        assert "מהירות" not in text
        assert "⚡" not in text


def test_format_progress_non_numeric_eta_is_silently_omitted():
    for junk_eta in ("", "unknown", [], {}):
        text = format_progress(PHASE, transferred=50, total=100, eta=junk_eta)
        assert "זמן משוער" not in text


def test_human_eta_non_numeric_returns_none():
    for junk in ("", "unknown", [], {}, object()):
        assert human_eta(junk) is None


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
    # bar + percent is on its own line (M11.19: not glued into the header
    # line with the size pair, and not glued into Hebrew prose either)
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
    bar_line = text.split("\n")[2]
    assert BAR_EMPTY not in bar_line  # the bar line is fully filled, no leftover empty cells


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
    """Table-driven per M11.12: at every boundary percent the bar has
    exactly BAR_WIDTH moon cells, the shown percent matches, and the bar
    never looks complete (10 full moons) below 100% - explicitly including
    99%, where a naive partial-cell scheme (as the previous bot used) could
    consume the one remaining slot and leave zero empty cells."""
    total = 1_000_000
    for percent in _BOUNDARY_PERCENTS:
        transferred = percent / 100 * total
        text = format_progress(PHASE, transferred=transferred, total=total)
        shown_percent = _percent(text)
        assert shown_percent == int(percent), f"percent={percent}: text shows {shown_percent}%"

        bar_line = text.split("\n")[2]
        total_moons = sum(bar_line.count(ch) for ch in _ALL_MOONS)
        assert total_moons == BAR_WIDTH, f"percent={percent}: bar has {total_moons} cells, not {BAR_WIDTH}"

        filled = bar_line.count(BAR_FILLED)
        empty = bar_line.count(BAR_EMPTY)

        if shown_percent < 100:
            assert empty > 0, f"percent={percent}: bar has no empty cell before 100% ({bar_line!r})"
            assert filled < BAR_WIDTH, f"percent={percent}: bar shows {BAR_WIDTH} full moons before 100% ({bar_line!r})"
        else:
            assert filled == BAR_WIDTH and empty == 0


def test_bar_at_99_percent_has_at_least_one_empty_cell():
    """The explicit case called out in the M11.12 round: the previous bot's
    algorithm let the partial-moon cell consume the last remaining slot at
    99%, leaving 9 full moons + 1 near-full partial + zero empty cells -
    indistinguishable from "done" at a glance. This must not regress."""
    text = format_progress(PHASE, transferred=99, total=100)
    bar_line = text.split("\n")[2]
    assert bar_line.count(BAR_EMPTY) >= 1
    assert bar_line.count(BAR_FILLED) < BAR_WIDTH


# --- M11.19: two-line layout (header = label + size; bar line = percent + bar)


def test_format_progress_splits_size_and_bar_onto_separate_lines():
    """The production report this round fixes: the owner explicitly asked
    for the size pair and the percent/bar to be on two separate lines, not
    joined into one. Line 0 is the phase, line 1 is "label: (size)", line 2
    is "percent% bar"."""
    text = format_progress(PHASE, transferred=45 * 1024 * 1024, total=100 * 1024 * 1024)
    lines = text.split("\n")
    assert len(lines) == 3
    assert lines[0] == PHASE
    assert "התקדמות" in lines[1]
    assert "(45.0MB/100.0MB)" in lines[1]
    assert "%" not in lines[1]
    assert BAR_FILLED not in lines[1] and BAR_EMPTY not in lines[1]
    assert lines[2].startswith("45%")
    assert BAR_FILLED in lines[2] or BAR_EMPTY in lines[2]
    assert "(" not in lines[2]  # the size pair stayed on the header line


def test_bar_line_has_no_strong_bidi_character():
    """Core mechanism of the M11.19 fix (see progress_format.py's module and
    `render_bar` docstrings): the percent/bar line carries no Hebrew label
    and no Latin unit letters, so it has no bidi-strong (L/R/AL) character
    at all. Per UAX#9 P2/P3 that means its paragraph direction falls back
    to the default (LTR) with no explicit directional control characters
    needed - confirmed separately against a real bidi engine (`python-bidi`
    0.6.11, disposable /tmp venv, not a project dependency)."""
    total = 1_000_000
    for percent in (0, 1, 50, 99, 100):
        transferred = percent / 100 * total
        text = format_progress(PHASE, transferred=transferred, total=total)
        bar_line = text.split("\n")[2]
        assert _has_no_strong_character(bar_line), (
            f"percent={percent}: bar line has a strong bidi character: {bar_line!r}"
        )


def test_header_line_opens_with_hebrew_label_and_carries_only_the_size_pair():
    text = format_progress(PHASE, transferred=45, total=100)
    header_line = text.split("\n")[1]
    assert _first_strong_is_rtl(header_line)
    assert header_line.index("התקדמות") < header_line.index("(")
    # NOT wrapped in backticks (Markdown inline code): that turns this run
    # into a `MessageEntityCode` whose offset/length is fixed against the
    # text as sent - production hit `EntityBoundsInvalidError` on edit from
    # exactly this (2026-09-30), which then froze all further progress
    # updates for the message. See progress_format.py's `format_progress`
    # for the full incident note.
    assert "`" not in header_line


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


def test_unknown_total_splits_label_and_volume_onto_separate_lines():
    """Same two-line principle as the known-total case (M11.19): the header
    line carries what's known (the label plus the "total unknown"
    qualifier), and a separate line carries the one real data point (bytes
    transferred so far) - there's no percent/bar to show, so that second
    line holds just the volume."""
    text = format_progress(PHASE, transferred=12 * 1024 * 1024, total=None)
    lines = text.split("\n")
    assert len(lines) == 3
    assert lines[0] == PHASE
    assert "הורד" in lines[1]
    assert "לא ידוע" in lines[1]
    assert "12.0MB" not in lines[1]
    assert lines[2] == f"{LRM}12.0MB"


def test_every_line_that_carries_a_hebrew_label_opens_with_a_strong_rtl_character():
    """The concrete failure mode this guards against: a plain-text bidi
    renderer (or a Telegram client that computes per-line paragraph
    direction) picks LTR for any line whose first *strong* character isn't
    Hebrew/Arabic - even if LRM is present later in the line. This holds for
    every line that actually carries Hebrew text. The bar/percent line (and,
    in unknown-total mode, the volume-only line) intentionally carry no
    Hebrew text at all and so intentionally fall outside this rule - see
    `test_bar_line_has_no_strong_bidi_character` and the module docstring
    for why that's the fix, not a regression."""
    text = format_progress(
        PHASE,
        transferred=45 * 1024 * 1024,
        total=100 * 1024 * 1024,
        speed=1.5 * 1024 * 1024,
        eta=36,
    )
    lines = text.split("\n")
    hebrew_lines = [line for line in lines if "התקדמות" in line or line == PHASE or "מהירות" in line or "משוער" in line]
    assert len(hebrew_lines) == 4  # phase, header, speed, eta - everything but the bar line
    for line in hebrew_lines:
        assert _first_strong_is_rtl(line), f"line does not open with a strong RTL character: {line!r}"

    unknown_total_text = format_progress(PHASE, transferred=12 * 1024 * 1024, total=None)
    unknown_lines = unknown_total_text.split("\n")
    assert _first_strong_is_rtl(unknown_lines[0])
    assert _first_strong_is_rtl(unknown_lines[1])  # "📥 הורד: (סך כולל לא ידוע)"
    # unknown_lines[2] (the bare volume, "<LRM>12.0MB") intentionally does not -
    # LRM itself is strong-L, by design (see progress_format.py's RTL note).

    # NOTE: a string-level check like this confirms the *bidi type* of each
    # line's leading character, which decides paragraph direction. It cannot
    # confirm how Telegram's actual clients (desktop/web/Android/iOS) render
    # the full multi-line message - that still requires an eyeball check in
    # each client against a real in-progress download/upload message.


# --- M11.19: exact match to the production report's requested rendering -----


def test_format_progress_matches_owner_reported_upload_example():
    """The exact case from the production report: an upload at 27.2MB of
    67.5MB (40%), 4.4MB/s, 9 seconds left. The owner's requested corrected
    rendering (two lines: header with label+size, then a separate
    percent+bar line) is reproduced here byte-for-byte, including the LRM
    marks before the speed/ETA numbers (invisible, but present in the
    owner's own pasted example)."""
    text = format_progress(
        "⬆️ מעלה לטלגרם...",
        transferred=27.2 * 1024 * 1024,
        total=67.5 * 1024 * 1024,
        speed=4.4 * 1024 * 1024,
        eta=9,
    )
    expected = (
        "⬆️ מעלה לטלגרם...\n"
        "📊 התקדמות: (27.2MB/67.5MB)\n"
        "40% 🌑🌑🌑🌑🌑🌑🌓🌕🌕🌕\n"
        f"⚡ מהירות: {LRM}4.4MB/s\n"
        f"⏱️ זמן משוער: {LRM}9 שניות"
    )
    assert text == expected


# --- M11.12: moon bar must never produce a Telegram entity -------------------


def test_moon_bar_produces_no_telegram_entities():
    """The 2026-09-30 production incident (see progress.py/format_progress
    docstrings) came from backticks around the bar turning it into a
    `MessageEntityCode`, whose offset/length then desynced from what
    Telegram actually stored and froze every later edit. Run the real
    Telethon markdown parser (the one `message.edit(text)` uses by default)
    over the generated text and confirm it never produces an entity, for
    every boundary percent plus the unknown-total and full-detail shapes."""
    from telethon.extensions import markdown

    texts_to_check = [
        format_progress(PHASE, transferred=p / 100 * 1_000_000, total=1_000_000) for p in _BOUNDARY_PERCENTS
    ] + [
        format_progress(PHASE, transferred=12 * 1024 * 1024, total=None),
        format_progress(
            PHASE,
            transferred=45 * 1024 * 1024,
            total=100 * 1024 * 1024,
            speed=1.5 * 1024 * 1024,
            eta=36,
        ),
    ]
    for text in texts_to_check:
        _parsed_text, entities = markdown.parse(text)
        assert entities == [], f"unexpected entities for {text!r}: {entities}"
        assert "`" not in text


# --- M11.12: monotonicity across a realistic increasing update sequence -----


def _moon_level(ch: str) -> int:
    return {BAR_EMPTY: 0, MOON_QUARTER: 1, MOON_HALF: 2, MOON_THREE_QUARTER: 3, BAR_FILLED: 4}[ch]


def _bar_fullness(bar: str) -> int:
    return sum(_moon_level(ch) for ch in bar)


def test_moon_bar_is_monotonic_across_an_increasing_update_sequence():
    """A real download reports an increasing, sometimes finely-spaced
    sequence of percentages. The bar's total "fullness" (sum of per-cell
    moon phase levels) must never decrease across such a sequence, even
    though the previous bot's naive partial-cell scheme could regress right
    where its cap kicked in near the top of the range."""
    total = 1_000_000
    percents = [p / 10 for p in range(1001)]  # 0.0, 0.1, ..., 100.0
    prev_fullness = -1
    for percent in percents:
        transferred = percent / 100 * total
        text = format_progress(PHASE, transferred=transferred, total=total)
        bar_line = text.split("\n")[2]
        bar = "".join(ch for ch in bar_line if ch in _ALL_MOONS)
        assert len(bar) == BAR_WIDTH
        fullness = _bar_fullness(bar)
        assert fullness >= prev_fullness, f"percent={percent}: bar fullness regressed ({bar!r})"
        prev_fullness = fullness
