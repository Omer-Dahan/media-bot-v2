"""One progress-block renderer shared by the download hooks (yt-dlp) and the
upload byte counter (`telegram/progress.py`'s `UploadProgress`), so both look
like the same bot: a phase line, then a size/caption line, then its own
bar/percent line, then speed and ETA - each omitted rather than shown as a
misleading "None"/0 when the underlying data isn't available yet (no
declared total, no speed sample, no ETA).

RTL note: LRM (U+200E) is itself a *strong* LTR character to the bidi
algorithm - putting it as a line's very first character makes that whole
line's paragraph direction LTR (the P2/P3 first-strong-character rule),
which is the opposite of what we want and produces a line that renders
left-aligned next to right-aligned Hebrew lines. So every line that opens
with a Hebrew label keeps that label as its first character (a strong RTL
character) - LRM only appears *after* such a label, where its job is
narrower: keep the digits/units that follow reading left-to-right instead
of picking up the paragraph's RTL order.

M11.19 (production report: the moon bar rendered with full/empty cells
"reversed" and no gap between cells): the *previous* version of this module
put the bar, percent, and size pair all on one line, after the Hebrew
label, wrapped in an explicit LTR embedding (U+202A/U+202C) with its
contents typed in reverse (size, percent, bar) so that the embedding's
single L2 reversal would land the bar next to the label. That was verified
correct *in theory* against a real bidi engine (`python-bidi` 0.6.11) at
the time - but real Telegram clients apparently do not reproduce that
derivation faithfully for this shape (production evidence: the owner saw
the bar in logical/typed order, un-reversed, exactly as if the embedding
characters had no effect). Rather than chase which client strips or
mishandles U+202A/U+202C, the fix removes the dependency on explicit
embedding characters entirely:

Re-running the *old* single-line construction through `python-bidi` 0.6.11
again for this round (both as a standalone line and embedded in the full
multi-line message) still reproduces the original "correct" derivation,
not the production symptom - meaning the gap between the two is a
real-client quirk this library can't surface (most plausibly: a Telegram
client not honoring U+202A/U+202C the way the bidi spec says, since
explicit directional-embedding/override characters are a known target for
sanitization by text renderers wary of RTL-spoofing attacks). Rather than
chase which client does what, the fix sidesteps the question entirely by
not depending on those control characters at all:

The bar/percent line is now its own line, with nothing else on it - no
Hebrew label, no size pair. Its content (digits, "%", and the moon emoji)
is bidi type EN/ET/ON - *no strong character at all*. Per UAX#9 P2/P3, a
paragraph with no strong L/R/AL character defaults to LTR - and every real
multi-line text renderer (Telegram's clients included, per how every line
of this formatter's output has always rendered independently in practice)
resolves paragraph direction per hard-line-break, not once for the whole
message. That means this line needs no explicit directional control
characters at all - it displays in exactly the order it's typed, on every
conformant renderer, with no reliance on embedding codes a client might
sanitize. Verified with `python-bidi` 0.6.11 (installed only in a
throwaway `/tmp` venv for this check, never a project dependency) by
running each line through `get_display` independently across the boundary
percents (0/1/50/99/100): `get_display(line) == line` in every case -
confirming this renders exactly as the owner's requested example shows it
("40% 🌑🌑🌑🌑🌑🌑🌓🌕🌕🌕", typed order, no reversal).

The header line (label + size pair, e.g. "📊 התקדמות: (27.2MB/67.5MB)") does
still open with the Hebrew label (strong RTL, same as before), with the
size pair following as plain text - no LTR embedding needed there either:
cross-checked with `python-bidi` 0.6.11 across a range of size pairs (zero
bytes, matching totals, KB/MB/GB units), the implicit bidi algorithm's N0
bracket-pair rule resolves the parenthesised "(a/b)" run correctly inside
an RTL paragraph on its own - wrapping it in an explicit embedding produced
a byte-for-byte identical result in every case tested, i.e. the embedding
was already a no-op for this shape and its removal changes nothing other
than deleting characters a client could mishandle.

Moon-to-moon spacing: the owner also reported "no space between" the
cells. No space character was added between them (see `render_bar` below -
unchanged from before). The owner's own corrected example also has no
space between the moons, which points to the "no space" complaint being a
symptom of the old bidi corruption (bar glyphs visually smeared into the
neighbouring percent/size digits) rather than a request to literally widen
each cell - and a verified identical match to that example is covered by
`test_format_progress_matches_owner_reported_upload_example` below.
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


def format_progress(
    phase_label: str,
    *,
    transferred: int | None = None,
    total: int | None = None,
    speed: float | None = None,
    eta: float | None = None,
) -> str:
    """`phase_label` (already Hebrew, e.g. "⬇️ מוריד...") followed by whichever
    of the size/bar/speed/ETA lines have real data. `total` missing or falsy
    means the caller couldn't determine a total (e.g. yt-dlp without a
    Content-Length) - shown as bytes-transferred-only, never a bar frozen at
    a fake percentage.

    M11.19: the size pair and the bar/percent are two separate lines, not
    one - see the module docstring for why (production report of a
    "reversed"-looking bar; the fix drops the explicit LTR-embedding trick
    the single-line version relied on, in favor of a bar line with no
    Hebrew label on it at all, which needs no explicit bidi control
    characters to render correctly)."""
    lines = [phase_label]

    if transferred is not None and total:
        # Single source of truth: this truncated percent feeds both the bar
        # fill and the "%" text, so they can never disagree with each other.
        percent = max(0.0, min(100.0, transferred / total * 100))
        percent_int = int(percent)
        bar = render_bar(percent_int)
        size_part = f"({human_size(transferred)}/{human_size(total)})"
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
        lines.append(f"📊 התקדמות: {size_part}")
        # Own line, no Hebrew label on it - see render_bar's docstring and
        # the module docstring for why that's what makes this render in
        # typed order with no explicit bidi control characters needed.
        lines.append(f"{percent_int}% {bar}")
    elif transferred is not None:
        lines.append("📥 הורד: (סך כולל לא ידוע)")
        lines.append(f"{LRM}{human_size(transferred)}")

    if _is_real_number(speed) and speed > 0 and math.isfinite(speed):
        lines.append(f"⚡ מהירות: {LRM}{human_size(speed)}/s")

    eta_text = human_eta(eta)
    if eta_text:
        lines.append(f"⏱️ זמן משוער: {LRM}{eta_text}")

    return "\n".join(lines)
