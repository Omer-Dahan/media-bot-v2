"""One progress-block renderer shared by the download hooks (yt-dlp) and the
upload byte counter (`telegram/progress.py`'s `UploadProgress`), so both look
like the same bot: a phase line, then a separator-framed block holding the
bar/percent line, the size line, speed and ETA - each omitted rather than
shown as a misleading "None"/0 when the underlying data isn't available yet
(no declared total, no speed sample, no ETA).

RTL note: LRM (U+200E) is itself a *strong* LTR character to the bidi
algorithm - putting it as a line's very first character makes that whole
line's paragraph direction LTR (the P2/P3 first-strong-character rule),
which is the opposite of what we want and produces a line that renders
left-aligned next to right-aligned Hebrew lines. So every line that opens
with a Hebrew label keeps that label as its first character (a strong RTL
character) - LRM only appears *after* such a label, where its job is
narrower: keep the digits/units that follow reading left-to-right instead
of picking up the paragraph's RTL order.

The bar/percent line carries no Hebrew label and no Latin unit letters, so
it has no bidi-strong (L/R/AL) character at all - per UAX#9 P2/P3 that
falls back to the default (LTR) with no explicit directional control
characters needed, so it always renders in exactly the order it's typed.

M11.20 (production report: the block "doesn't come together" - the size
sat in parentheses glued to the "progress:" label line, and the bar/percent
sat alone on a line in between, reading as a pile of disconnected lines
rather than one unit): this version frames the whole bar/size/speed/ETA
block between two separator lines (plain box-drawing characters, no strong
bidi character either, same reasoning as the bar line), matching the
previous bot's layout - so Telegram renders it as one visual card instead
of loose lines. The phase line sits above the top separator; everything
measured (bar, size, speed, ETA) sits between the separators.
"""

from __future__ import annotations

import math
from typing import Any

from media_bot_v2.telegram.texts import human_size

BAR_WIDTH = 10
BAR_FILLED = "🌕"  # full moon
BAR_EMPTY = "🌑"  # new moon
MOON_QUARTER = "🌒"
MOON_HALF = "🌓"
MOON_THREE_QUARTER = "🌔"
LRM = "\u200e"  # left-to-right mark
SEPARATOR = "\u2501" * 18  # box-drawing: no bidi-strong character, renders the same either direction

# A known total at/above this size starts the request straight in full
# (bar) render mode - worth drawing a bar immediately for a download this big.
PROGRESS_BAR_MIN_BYTES = 50 * 1024 * 1024
# Otherwise a request switches from compact (plain-text) to full (bar) once
# it has run this long - short-lived downloads never show a bar at all.
PROGRESS_BAR_MIN_SECONDS = 5.0


def _partial_moon(remainder: float) -> str:
    """Maps how far into its cell the current fill sits (0-1, exclusive of
    0) to one of the three waxing phases - same thresholds as the previous
    bot's moon bar."""
    if remainder >= 0.67:
        return MOON_THREE_QUARTER
    if remainder >= 0.34:
        return MOON_HALF
    return MOON_QUARTER


def render_bar(percent: float, width: int = BAR_WIDTH) -> str:
    """`width`-cell moon-phase bar, `percent` (0-100, clamped) of it filled.

    Only `width - 1` cells are ever used to represent progress short of
    100 - the last cell is always held back as a new moon - so the bar can
    never read as "basically done" before it actually is. A naive
    floor+remainder split across all `width` cells (as the previous bot
    did) lets the fractional/partial cell consume the one remaining slot at
    high percentages (e.g. 99% -> 9 full + 1 near-full partial + 0 empty
    cells), which looks indistinguishable from "done" at a glance.
    Reserving one cell up front guarantees at least one empty cell remains
    for every percent below 100, without ever letting the bar's fill
    *decrease* as percent rises (the reservation is constant, not a late
    correction).

    Returned empty-to-full, not full-to-empty: `format_progress` places this
    on its own line with nothing else (no Hebrew label), which per UAX#9
    P2/P3 defaults that line's paragraph direction to LTR (no strong L/R/AL
    character is present at all - digits, "%", and the moon emoji are all
    weak/neutral types) - verified against `python-bidi` 0.6.11 to render
    in exactly this typed order, with zero reversal, see
    tests/test_progress_format.py. Typing empty-first/full-last therefore
    reads left-to-right on screen as "fills toward the right" as percent
    increases, matching the order the production report asked for; typing
    full-first (as the previous bot did) would instead show the bar
    draining from left to right as percent rises."""
    clamped = max(0.0, min(100.0, percent))
    if clamped >= 100:
        return BAR_FILLED * width
    if width < 2:
        return BAR_EMPTY * width
    active_width = width - 1
    raw = clamped / 100 * active_width
    filled = int(raw)
    remainder = raw - filled
    partial = _partial_moon(remainder) if remainder > 0 else ""
    empty = width - filled - (1 if partial else 0)
    return BAR_EMPTY * empty + partial + BAR_FILLED * filled


def _is_real_number(value: object) -> bool:
    """`speed`/`eta` sometimes arrive as `""` (an unset yt-dlp hook field) or
    other non-numeric junk rather than `None` - `"" > 0` raises `TypeError`,
    so every numeric comparison below must be guarded by this first."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def human_eta(seconds: float | None) -> str | None:
    if not _is_real_number(seconds) or not seconds or seconds <= 0 or not math.isfinite(seconds):
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} שניות"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d} דקות"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d} שעות"


def compact_mode_for(progress: Any, total: int | None) -> bool:
    """Whether `progress` (normally a `MessageProgressReporter`) wants the
    compact (no-bar) render for this call - see
    `MessageProgressReporter.progress_mode_compact`. Reporters/test doubles
    that don't implement it keep the pre-compact-mode full bar render."""
    query = getattr(progress, "progress_mode_compact", None)
    if query is None:
        return False
    return query(total)


def format_progress(
    phase_label: str,
    *,
    transferred: int | None = None,
    total: int | None = None,
    speed: float | None = None,
    eta: float | None = None,
    compact: bool = False,
) -> str:
    """`phase_label` (already Hebrew, e.g. "⬇️ מוריד...") followed by whichever
    of the size/bar/speed/ETA lines have real data. `total` missing or falsy
    means the caller couldn't determine a total (e.g. yt-dlp without a
    Content-Length) - shown as bytes-transferred-only, never a bar frozen at
    a fake percentage.

    `compact=True` (short/fast downloads, see `PROGRESS_BAR_MIN_BYTES`/
    `PROGRESS_BAR_MIN_SECONDS`) drops the moon-bar glyph and renders the
    percent as plain text instead - a bar that's only ever on screen for a
    couple of seconds is noise, not signal. The size line is unchanged
    either way.

    M11.20: the bar/percent, size, speed and ETA lines are framed between
    two separator lines (see `SEPARATOR`/module docstring) so they read as
    one block rather than loose lines; the phase label stays above the top
    separator. When there's no data at all (no `transferred`), the block is
    empty and omitted entirely - no bare separator pair."""
    lines = [phase_label]
    block: list[str] = []

    if transferred is not None and total:
        # Single source of truth: this truncated percent feeds both the bar
        # fill and the "%" text, so they can never disagree with each other.
        percent = max(0.0, min(100.0, transferred / total * 100))
        percent_int = int(percent)
        # NOT wrapped in backticks (Markdown inline code): that turns this
        # run into a `MessageEntityCode` whose offset/length is fixed
        # against the text as *we* send it - if Telegram strips or otherwise
        # normalizes characters in transit (observed in production for this
        # exact line, 2026-09-30: `EditMessageRequest` rejected with
        # `EntityBoundsInvalidError`, "length is zero or out of the
        # boundaries of the string"), the entity now points past the end of
        # the text Telegram actually stored, and every subsequent edit to
        # that message is rejected too. Plain text has no entity to
        # invalidate, so it can't reproduce that failure.
        if compact:
            # Plain percent, no bar glyph - still no strong L/R/AL
            # character, so this keeps rendering in typed order per
            # UAX#9 P2/P3 same as the full bar/percent line below.
            block.append(f"{percent_int}%")
        else:
            bar = render_bar(percent_int)
            # Own line, no Hebrew label on it - see render_bar's docstring and
            # the module docstring for why that's what makes this render in
            # typed order with no explicit bidi control characters needed.
            block.append(f"{bar} {percent_int}%")
        block.append(f"📊 התקדמות: {human_size(transferred)}/{human_size(total)}")
    elif transferred is not None:
        block.append("📥 הורד: (סך כולל לא ידוע)")
        block.append(f"{LRM}{human_size(transferred)}")

    if _is_real_number(speed) and speed > 0 and math.isfinite(speed):
        block.append(f"⚡ מהירות: {LRM}{human_size(speed)}/s")

    eta_text = human_eta(eta)
    if eta_text:
        block.append(f"⏱️ זמן משוער: {LRM}{eta_text}")

    if block:
        lines.append(SEPARATOR)
        lines.extend(block)
        lines.append(SEPARATOR)

    return "\n".join(lines)
