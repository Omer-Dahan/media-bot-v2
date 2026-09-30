"""M11/M11.3: the shared bar/percent/size/speed/ETA renderer used by both the
download (yt-dlp hook) and upload (byte-count) progress messages."""

import re
import unicodedata

from media_bot_v2.telegram.progress_format import (
    BAR_EMPTY,
    BAR_FILLED,
    BAR_WIDTH,
    EMBED_LTR,
    LRM,
    MOON_HALF,
    MOON_QUARTER,
    MOON_THREE_QUARTER,
    POP_EMBED,
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


# --- a small, hand-verified UAX#9 special case, used below to check real
# --- visual (not just logical/source-order) placement --------------------
#
# `python-bidi`/`libfribidi` are not installed in this project (no new
# dependencies for this round) so this suite cannot shell out to a real bidi
# engine at test time. Per the round's instructions, the fix was instead
# cross-checked *during development* against a real, standards-conformant
# implementation (`python-bidi` 0.6.11, installed only in a disposable
# throwaway venv outside the repo, never added as a dependency here) - see
# progress_format.py's module docstring for the reasoning and the exact
# before/after visual strings that verification produced.
#
# The check below re-derives the same conclusion from the Unicode Bidirectional
# Algorithm (UAX#9) rules directly, for the one restricted shape this
# formatter actually produces: a base-RTL paragraph consisting of
# [R/neutral label][EMBED_LTR][L/EN/neutral content, no R][POP_EMBED].
#
# For that shape specifically (no nested strong-R inside the embedding, no
# other embeddings): explicit-level rule X5a assigns the embedded span one
# level higher than the label; implicit rule I2 bumps the (odd) label's
# level no further since it is already R; L2 ("from the highest level down
# to the lowest odd level, reverse each contiguous run at or above that
# level") then reverses the embedded span once (its own, higher level) and
# reverses the *whole line* once more (the lowest odd level present, the
# label's). Two reversals of the embedded span cancel out, so it renders in
# its typed order; the single remaining reversal of the whole line is what
# swaps the label and the embedded span's relative screen position. Net
# result: screen order (left to right) is [embedded content, in typed
# order] followed by [label] - i.e. reading right-to-left, label first,
# then the embedded content in the order it was typed.
def _visual_screen_order_ltr_embed(line: str) -> list[str]:
    """Split `line` into [text before EMBED_LTR, text inside EMBED_LTR..POP_EMBED]
    and return them in left-to-right screen order per the derivation above.
    Only valid when the embedded span has no strong-RTL characters in it
    (asserted here, since that is the precondition the derivation relies on)."""
    before, _, rest = line.partition(EMBED_LTR)
    inside, _, _after = rest.partition(POP_EMBED)
    for ch in inside:
        assert unicodedata.bidirectional(ch) not in ("R", "AL"), (
            f"{ch!r} inside the embedded span is strong-RTL; "
            "the special-case derivation above does not apply"
        )
    return [inside, before]


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
    full-cells-last (see its docstring for the bidi reasoning): the
    *last*-typed character of `format_progress`'s embedded run is what ends
    up adjacent to the RTL label on screen, and full moons belong there
    ("fills from the right"), not the empty ones. This is the exact
    ordering bug the previous bot's `full-first` construction would have
    reproduced were it copied as-is."""
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

        bar_line = text.split("\n")[1]
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
    bar_line = text.split("\n")[1]
    assert bar_line.count(BAR_EMPTY) >= 1
    assert bar_line.count(BAR_FILLED) < BAR_WIDTH


# --- RTL: the bar/percent/size and speed/ETA lines carry an LTR override ----


def test_bar_line_opens_with_hebrew_label_then_embedded_ltr_run():
    text = format_progress(PHASE, transferred=45, total=100)
    bar_line = text.split("\n")[1]
    assert not bar_line.startswith(EMBED_LTR)  # a strong-L char can't open an RTL line
    assert EMBED_LTR in bar_line and POP_EMBED in bar_line
    # Hebrew label precedes the embedded numeric/bar run
    assert bar_line.index("התקדמות") < bar_line.index(EMBED_LTR)
    # NOT wrapped in backticks (Markdown inline code): that turns this run
    # into a `MessageEntityCode` whose offset/length is fixed against the
    # text as sent - production hit `EntityBoundsInvalidError` on edit from
    # exactly this (2026-09-30), which then froze all further progress
    # updates for the message. See progress_format.py's `format_progress`
    # for the full incident note.
    assert "`" not in bar_line
    inside = bar_line.partition(EMBED_LTR)[2].partition(POP_EMBED)[0]
    # *typed* (logical/source) order inside the embedded run is size, then
    # percent, then bar - the reverse of reading order. This is what the
    # bidi visual-order test below (which is the one that actually matters)
    # confirms lands bar-closest-to-label on screen; see the module
    # docstring for why a naive bar-first typed order does not.
    assert inside.index("(") < inside.index("%") < inside.index(BAR_FILLED)

def test_bar_line_visual_order_is_label_then_bar_then_percent_then_sizes():
    """The actual finding: cross-checked against a real bidi engine (see
    module docstring and the header comment above `_visual_screen_order_ltr_embed`),
    a bare LRM left the bar thrown to the far visual edge, disconnected from
    the label, with the parenthesised size pair mirrored and broken apart.
    The fix must produce, right-to-left (the order a person actually reads
    this line): label, then bar, then percent, then sizes."""
    text = format_progress(PHASE, transferred=45 * 1024 * 1024, total=100 * 1024 * 1024)
    bar_line = text.split("\n")[1]
    content, label = _visual_screen_order_ltr_embed(bar_line)

    # `content` is the embedded span, screen-ordered left-to-right (per the
    # derivation above, an embedded run with no strong-RTL inside renders in
    # its typed order). Left-to-right on screen it is [sizes][percent][bar],
    # i.e. bar sits at its *right* edge - immediately next to the label
    # (which the derivation places further right still). Reading
    # right-to-left, that is label, then bar, then percent, then sizes.
    assert content.index("(45.0MB/100.0MB)") < content.index("45%") < content.index(BAR_FILLED)
    assert "התקדמות" in label


def test_bar_line_visual_order_holds_at_boundary_percents():
    for percent in (0, 1, 50, 99, 100):
        total = 1_000_000
        transferred = percent / 100 * total
        text = format_progress(PHASE, transferred=transferred, total=total)
        bar_line = text.split("\n")[1]
        content, label = _visual_screen_order_ltr_embed(bar_line)
        bar_char = BAR_FILLED if BAR_FILLED in content else BAR_EMPTY
        assert content.index(f"{percent}%") < content.rindex(bar_char)
        assert "התקדמות" in label


def test_moon_bar_full_cells_land_adjacent_to_the_label_not_empty_cells():
    """Cross-checked against a real bidi engine (`python-bidi` 0.6.11, same
    disposable-venv verification method as the rest of this module - not a
    project dependency): per `_visual_screen_order_ltr_embed`'s derivation,
    `content`'s *last* character is the one that lands immediately adjacent
    to the RTL label on screen. For "fills from the right" to actually
    read correctly, that adjacent character must be a full moon once any
    progress exists - not an empty one. Naively typing the bar full-first/
    empty-last (as the previous bot's `moon_progress_bar` did) would put an
    *empty* cell there instead, making the bar look like it fills away from
    the label rather than toward it."""
    total = 1_000_000
    for percent in (45.1, 49.9, 50, 95, 99, 99.9):
        transferred = percent / 100 * total
        text = format_progress(PHASE, transferred=transferred, total=total)
        bar_line = text.split("\n")[1]
        content, label = _visual_screen_order_ltr_embed(bar_line)
        assert content[-1] == BAR_FILLED, (
            f"percent={percent}: character adjacent to the label is {content[-1]!r}, expected a full moon"
        )
        assert "התקדמות" in label

    # At 0%, there is no progress to show adjacent to the label - the
    # reserved-empty design correctly leaves an empty moon there instead.
    text = format_progress(PHASE, transferred=0, total=total)
    bar_line = text.split("\n")[1]
    content, _label = _visual_screen_order_ltr_embed(bar_line)
    assert content[-1] == BAR_EMPTY


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
        bar_line = text.split("\n")[1]
        bar = "".join(ch for ch in bar_line if ch in _ALL_MOONS)
        assert len(bar) == BAR_WIDTH
        fullness = _bar_fullness(bar)
        assert fullness >= prev_fullness, f"percent={percent}: bar fullness regressed ({bar!r})"
        prev_fullness = fullness
