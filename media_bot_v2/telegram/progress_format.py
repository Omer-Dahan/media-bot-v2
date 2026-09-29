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
*after* that label, where its job is narrower: keep the digits/bar
glyphs/units that follow reading left-to-right instead of picking up the
paragraph's RTL order.
"""

from __future__ import annotations

import math

from media_bot_v2.telegram.texts import human_size

BAR_WIDTH = 10
BAR_FILLED = "█"  # █
BAR_EMPTY = "░"  # ░
LRM = "‎"  # left-to-right mark


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


def human_eta(seconds: float | None) -> str | None:
    if not seconds or seconds <= 0 or not math.isfinite(seconds):
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
        lines.append(
            f"📊 התקדמות: {LRM}{bar} {percent_int}% ({human_size(transferred)}/{human_size(total)})"
        )
    elif transferred is not None:
        lines.append(f"📥 הורד: {LRM}{human_size(transferred)} (סך כולל לא ידוע)")

    if speed is not None and speed > 0 and math.isfinite(speed):
        lines.append(f"⚡ מהירות: {LRM}{human_size(speed)}/s")

    eta_text = human_eta(eta)
    if eta_text:
        lines.append(f"⏱️ זמן משוער: {LRM}{eta_text}")

    return "\n".join(lines)
