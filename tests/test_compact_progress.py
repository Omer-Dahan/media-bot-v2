"""Compact (text-only) progress render until PROGRESS_BAR_MIN_SECONDS or
PROGRESS_BAR_MIN_BYTES: see MessageProgressReporter.progress_mode_compact."""

from media_bot_v2.telegram.progress import MessageProgressReporter
from media_bot_v2.telegram.progress_format import (
    BAR_EMPTY,
    BAR_FILLED,
    MOON_HALF,
    MOON_QUARTER,
    MOON_THREE_QUARTER,
    PROGRESS_BAR_MIN_BYTES,
    PROGRESS_BAR_MIN_SECONDS,
    format_progress,
)

MOON_GLYPHS = {BAR_EMPTY, MOON_QUARTER, MOON_HALF, MOON_THREE_QUARTER, BAR_FILLED}


class _Message:
    def __init__(self):
        self.edits: list[str] = []

    async def edit(self, text: str) -> None:
        self.edits.append(text)


class _Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_compact_render_has_no_moon_glyphs_or_bar():
    text = format_progress("⬇️ מוריד", transferred=1024, total=10 * 1024, compact=True)
    assert not any(glyph in text for glyph in MOON_GLYPHS)


def test_fresh_reporter_small_total_is_compact():
    reporter = MessageProgressReporter(_Message(), clock=_Clock(0.0))
    assert reporter.progress_mode_compact(total=1024) is True


def test_graduates_to_full_after_min_seconds_and_stays_full():
    clock = _Clock(0.0)
    reporter = MessageProgressReporter(_Message(), clock=clock)
    assert reporter.progress_mode_compact(total=None) is True

    clock.value = PROGRESS_BAR_MIN_SECONDS
    assert reporter.progress_mode_compact(total=None) is False

    # One-way: even if a later call reports elapsed/total back down, it stays full.
    clock.value = 0.0
    assert reporter.progress_mode_compact(total=0) is False


def test_known_large_total_is_full_from_first_render():
    reporter = MessageProgressReporter(_Message(), clock=_Clock(0.0))
    assert reporter.progress_mode_compact(total=PROGRESS_BAR_MIN_BYTES) is False


async def test_last_text_dedupe_across_compact_to_full_switch():
    clock = _Clock(0.0)
    message = _Message()
    reporter = MessageProgressReporter(message, clock=clock)

    compact_text = format_progress(
        "⬇️ מוריד",
        transferred=1024,
        total=2048,
        compact=reporter.progress_mode_compact(total=2048),
    )
    await reporter.update(compact_text)
    await reporter.update(compact_text)  # identical - no extra edit

    clock.value = PROGRESS_BAR_MIN_SECONDS
    full_text = format_progress(
        "⬇️ מוריד",
        transferred=1536,
        total=2048,
        compact=reporter.progress_mode_compact(total=2048),
    )
    await reporter.update(full_text)

    assert message.edits == [compact_text, full_text]
    assert compact_text != full_text
