"""One progress-block renderer shared by the download hooks (yt-dlp) and the
upload byte counter (`telegram/progress.py`'s `UploadProgress`), so both look
like the same bot: a phase line, then a bar/percent/size line, then speed and
ETA - each omitted rather than shown as a misleading "None"/0 when the
underlying data isn't available yet (no declared total, no speed sample, no
ETA).

RTL note: LRM (U+200E) is itself a *strong* LTR character to the bidi
algorithm - putting it as a line's very first character makes that whole
line's paragraph direction LTR (the P2/P3 first-strong-character rule),
which is the opposite of what we want and produces a line that renders
left-aligned next to right-aligned Hebrew lines. So every line here opens
with a real Hebrew label (a strong RTL character) - the LRM only appears
*after* that label, where its job is narrower: keep the digits/units that
follow reading left-to-right instead of picking up the paragraph's RTL
order.

The bar/percent/size line needs more than LRM, and is handled differently.
A bare LRM only sets the *first-strong-character* context used by the W7
weak-type-resolution rule; it does not open a real directional run. With a
mix of neutral bar glyphs (moon emoji are bidi type ON, same as the block
glyphs they replaced - not L), digits, and a parenthesised size pair, that
leaves multiple independent neutral runs for the bidi algorithm to resolve
on its own - and per UAX#9 N0 (bracket pairs)
plus L2 (reordering by level) it does NOT keep them together: cross-checked
against a real bidi engine (`python-bidi` 0.6.11, not a project dependency -
installed only in a throwaway venv for this verification, never shipped),
the closing parenthesis ends up mirrored and stranded at the far visual
edge, disconnected from its digits, while the bar renders in front of the
label instead of tucked beside it. See tests/test_progress_format.py for
the verified before/after visual order and the reasoning behind the fix
below (`EMBED_LTR`/`POP_EMBED`, U+202A/U+202C): wrapping the whole
bar+percent+size run in one explicit LTR embedding turns it into a single
run the bidi algorithm treats atomically, and typing that run's *contents*
in reverse (size, percent, bar) is what lands bar-closest-to-label once the
embedding's single reversal flips the whole run back around the label.
"""

from __future__ import annotations

import math

from media_bot_v2.telegram.texts import human_size

BAR_WIDTH = 10
BAR_FILLED = "🌕"  # full moon
BAR_EMPTY = "🌑"  # new moon
MOON_QUARTER = "🌒"
MOON_HALF = "🌓"
MOON_THREE_QUARTER = "🌔"
LRM = "\u200e"  # left-to-right mark
EMBED_LTR = "\u202a"  # left-to-right embedding
POP_EMBED = "\u202c"  # pop directional formatting


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

    Returned empty-to-full, not full-to-empty: this string is placed as the
    *last* thing typed inside `format_progress`'s LTR-embedded run, and
    that run sits immediately after the Hebrew label - per UAX#9 L2, the
    run's single reversal (against the RTL label) lands its *last-typed*
    character adjacent to the label and its *first-typed* character at the
    far edge (verified against `python-bidi` 0.6.11, see
    tests/test_progress_format.py). Typing full moons last is what makes
    them land touching the label ("fills from the right"); typing them
    first (as the previous bot did) puts the empty cells next to the label
    instead, which reads as the bar filling from the wrong end."""
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


def format_progress(
    phase_label: str,
    *,
    transferred: int | None = None,
    total: int | None = None,
    speed: float | None = None,
    eta: float | None = None,
) -> str:
    """`phase_label` (already Hebrew, e.g. "⬇️ מוריד...") followed by whichever
    of the bar/size/speed/ETA lines have real data. `total` missing or falsy
    means the caller couldn't determine a total (e.g. yt-dlp without a
    Content-Length) - shown as bytes-transferred-only, never a bar frozen at
    a fake percentage."""
    lines = [phase_label]

    if transferred is not None and total:
        # Single source of truth: this truncated percent feeds both the bar
        # fill and the "%" text, so they can never disagree with each other.
        percent = max(0.0, min(100.0, transferred / total * 100))
        percent_int = int(percent)
        bar = render_bar(percent_int)
        size_part = f"({human_size(transferred)}/{human_size(total)})"
        # Typed in reverse (size, percent, bar) - see the module docstring:
        # one LTR-embedded run reverses once against the RTL label, so its
        # *contents* must be pre-reversed for "bar" to land closest to the
        # label. NOT wrapped in backticks (Markdown inline code): that turns
        # this run into a `MessageEntityCode` whose offset/length is fixed
        # against the text as *we* send it - if Telegram strips or otherwise
        # normalizes characters in transit (observed in production for this
        # exact line, 2026-09-30: `EditMessageRequest` rejected with
        # `EntityBoundsInvalidError`, "length is zero or out of the
        # boundaries of the string"), the entity now points past the end of
        # the text Telegram actually stored, and every subsequent edit to
        # that message is rejected too. A bare LTR-embedded run has no
        # entity to invalidate, so it can't reproduce that failure - the bar
        # loses monospace alignment on proportional fonts, which is a purely
        # cosmetic tradeoff against a bug that froze progress updates for
        # the rest of the request.
        lines.append(f"📊 התקדמות: {EMBED_LTR}{size_part} {percent_int}% {bar}{POP_EMBED}")
    elif transferred is not None:
        lines.append(f"📥 הורד: {LRM}{human_size(transferred)} (סך כולל לא ידוע)")

    if _is_real_number(speed) and speed > 0 and math.isfinite(speed):
        lines.append(f"⚡ מהירות: {LRM}{human_size(speed)}/s")

    eta_text = human_eta(eta)
    if eta_text:
        lines.append(f"⏱️ זמן משוער: {LRM}{eta_text}")

    return "\n".join(lines)
