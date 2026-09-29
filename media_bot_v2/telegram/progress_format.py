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
mix of neutral bar glyphs (█/░ are bidi type ON, not L), digits, and a
parenthesised size pair, that leaves multiple independent neutral runs for
the bidi algorithm to resolve on its own - and per UAX#9 N0 (bracket pairs)
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
BAR_FILLED = "█"  # █
BAR_EMPTY = "░"  # ░
LRM = "\u200e"  # left-to-right mark
EMBED_LTR = "\u202a"  # left-to-right embedding
POP_EMBED = "\u202c"  # pop directional formatting


def render_bar(percent: float, width: int = BAR_WIDTH) -> str:
    """`width`-character bar, `percent` (0-100, clamped) of it filled.

    Floors rather than rounds, and never shows a full bar for anything
    short of 100 - a rounded fill (e.g. 95% -> round(9.5) -> 10/10) makes
    the bar look complete while the text still says "95%"."""
    clamped = max(0.0, min(100.0, percent))
    if clamped >= 100:
        return BAR_FILLED * width
    filled = min(int((clamped * width) // 100), width - 1)
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


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
        # label. Also wrapped in backticks (Markdown inline code - this
        # bot's messages use Telethon's default `parse_mode="md"`, not HTML)
        # so the bar renders in a monospace font: proportional fonts on some
        # Android/iOS clients give █/░ uneven widths, breaking bar alignment.
        lines.append(f"📊 התקדמות: `{EMBED_LTR}{size_part} {percent_int}% {bar}{POP_EMBED}`")
    elif transferred is not None:
        lines.append(f"📥 הורד: {LRM}{human_size(transferred)} (סך כולל לא ידוע)")

    if _is_real_number(speed) and speed > 0 and math.isfinite(speed):
        lines.append(f"⚡ מהירות: {LRM}{human_size(speed)}/s")

    eta_text = human_eta(eta)
    if eta_text:
        lines.append(f"⏱️ זמן משוער: {LRM}{eta_text}")

    return "\n".join(lines)
