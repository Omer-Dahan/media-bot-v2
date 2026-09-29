"""One progress-block renderer shared by the download hooks (yt-dlp) and the
upload byte counter (`telegram/progress.py`'s `UploadProgress`), so both look
like the same bot: a phase line, then a bar/percent/size line, then speed and
ETA - each omitted rather than shown as a misleading "None"/0 when the
underlying data isn't available yet (no declared total, no speed sample, no
ETA).

RTL note: the bar/percent/size line and the speed/ETA lines mix Hebrew text
with left-to-right content (the bar glyphs, digits, "MB", "/s"). Each such
line is its own line (never inlined into Hebrew prose) and is prefixed with
U+200E (LEFT-TO-RIGHT MARK) so the bidi algorithm anchors it as an LTR run
instead of leaving the reading order to whatever surrounds it.
"""

from __future__ import annotations

from media_bot_v2.telegram.texts import human_size

BAR_WIDTH = 10
BAR_FILLED = "█"  # █
BAR_EMPTY = "░"  # ░
LRM = "‎"  # left-to-right mark


def render_bar(percent: float, width: int = BAR_WIDTH) -> str:
    """`width`-character bar, `percent` (0-100, clamped) of it filled."""
    clamped = max(0.0, min(100.0, percent))
    filled = round(width * clamped / 100)
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


def human_eta(seconds: float | None) -> str | None:
    if not seconds or seconds <= 0:
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
        percent = max(0.0, min(100.0, transferred / total * 100))
        bar = render_bar(percent)
        lines.append(f"{LRM}{bar} {int(percent)}% ({human_size(transferred)}/{human_size(total)})")
    elif transferred is not None:
        lines.append(f"{LRM}{human_size(transferred)} (סך כולל לא ידוע)")

    if speed:
        lines.append(f"⚡ מהירות: {LRM}{human_size(speed)}/s")

    eta_text = human_eta(eta)
    if eta_text:
        lines.append(f"⏱️ זמן משוער: {LRM}{eta_text}")

    return "\n".join(lines)
