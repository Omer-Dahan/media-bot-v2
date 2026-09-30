"""Tests for M11.9: the frozen-progress-bar and undelivered-summary production
incident (2026-09-30) - see progress_format.py's `format_progress` and
progress.py's content-error handling for the full incident notes.

Production log excerpt that drove this round:
    WARNING  Progress message is not editable (EntityBoundsInvalidError: ...
             (caused by EditMessageRequest)); dropping further non-terminal edits
    WARNING  Failed to deliver fallback terminal message '✅ **הושלם** ...'
             exc=ReplyMarkupInvalidError: ... (caused by SendMessageRequest)
    ERROR    Terminal message ... could not be delivered (both edit and
             fallback failed); deleting stale progress message
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import EntityBoundsInvalidError, ReplyMarkupInvalidError
from telethon.extensions import markdown
from telethon.tl.types import ReplyInlineMarkup

from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress import _CLEAR_BUTTONS, MessageProgressReporter
from media_bot_v2.telegram.progress_format import format_progress

# =============================================================================
# A.3 - real Telethon-parser entity-bounds validation for every progress /
# summary text the bot can produce. This is the check the ticket says was
# missing: the fakes elsewhere in this test suite implement `edit(text)`
# without ever validating markdown entities, so a text that silently builds
# invalid entities (e.g. the removed backtick-wrapped bar span) sailed
# through the whole test suite despite failing against real Telegram.
# =============================================================================


def _assert_valid_entities(text: str) -> None:
    parsed_text, entities = markdown.parse(text)
    utf16_len = len(parsed_text.encode("utf-16-le")) // 2
    for entity in entities:
        assert entity.length > 0, (text, entity)
        assert entity.offset >= 0, (text, entity)
        assert entity.offset + entity.length <= utf16_len, (text, entity)


PROGRESS_SAMPLES = [
    format_progress(f"⬇️ {texts.DOWNLOADING}", transferred=0, total=98_765_432, speed=None, eta=None),
    format_progress(f"⬇️ {texts.DOWNLOADING}", transferred=44_444_444, total=98_765_432, speed=1_234_567, eta=45),
    format_progress(f"⬇️ {texts.DOWNLOADING}", transferred=93_827_160, total=98_765_432, speed=9_999_999, eta=1),
    format_progress(f"⬆️ {texts.UPLOADING}", transferred=98_765_432, total=98_765_432, speed=5_000_000, eta=0),
    format_progress(f"⬇️ {texts.DOWNLOADING}", transferred=1_234_567, total=None),
]

SUMMARY_SAMPLES = [
    texts.DOWNLOAD_DONE,
    texts.CACHE_DELIVERY_SUMMARY,
    texts.DOWNLOAD_FROM_CACHE,
    texts.QUEUE_WAIT,
    texts.PING_MESSAGE,
    texts.FLOOD_WAIT_MESSAGE.format(seconds=17),
    texts.format_download_summary(
        quality_label="1080p", duration_seconds=245, size_bytes=87_654_321, elapsed_seconds=63, from_cache=False
    ),
    texts.format_download_summary(is_audio=True, duration_seconds=180, size_bytes=4_500_000, elapsed_seconds=8),
    texts.format_download_summary(from_cache=True, quality_label="720p", elapsed_seconds=1),
    texts.format_download_summary(),
    texts.START,
    texts.HELP,
]


@pytest.mark.parametrize("text", PROGRESS_SAMPLES, ids=range(len(PROGRESS_SAMPLES)))
def test_progress_bar_texts_produce_valid_entities(text):
    """0%, mid-download, 95%-like, 100%, and no-declared-total - the exact
    shapes a real yt-dlp/provider download hook produces."""
    _assert_valid_entities(text)


@pytest.mark.parametrize("text", SUMMARY_SAMPLES, ids=range(len(SUMMARY_SAMPLES)))
def test_summary_and_static_texts_produce_valid_entities(text):
    _assert_valid_entities(text)


def test_progress_bar_fuzz_produces_valid_entities():
    """Broad fuzz across transferred/total/speed/eta combinations - a belt
    and suspenders check that no byte-size or timing value can reintroduce
    an invalid-entity regression in `format_progress`."""
    import random

    rng = random.Random(20260930)
    for _ in range(2000):
        total = rng.choice([None, rng.randint(1, 5_000_000_000)])
        transferred = rng.randint(0, total) if total else rng.randint(0, 5_000_000_000)
        speed = rng.choice([None, 0, rng.uniform(0, 50_000_000)])
        eta = rng.choice([None, 0, rng.uniform(0, 100_000)])
        text = format_progress(f"⬇️ {texts.DOWNLOADING}", transferred=transferred, total=total, speed=speed, eta=eta)
        _assert_valid_entities(text)


def test_progress_bar_line_carries_no_markdown_entity():
    """The concrete fix for the production incident: the bar/percent/size
    line must not be wrapped in backticks (or any other markdown delimiter)
    at all, so there is no `MessageEntityCode` whose offset/length Telegram
    can ever reject - regardless of what it does with the invisible
    directional-embedding characters around the bar."""
    text = format_progress(f"⬇️ {texts.DOWNLOADING}", transferred=45, total=100)
    _, entities = markdown.parse(text)
    assert entities == []


# =============================================================================
# A.2 - content-error safety net: an edit rejected for its *content*
# (entities or reply_markup) must be retried immediately as plain text
# instead of latching `_uneditable` and dropping every future update.
# =============================================================================


async def test_entity_bounds_error_retries_as_plain_text_and_keeps_editing():
    message = MagicMock()
    message.edit = AsyncMock(
        side_effect=[
            EntityBoundsInvalidError(None),  # first attempt: rejected for entities
            None,  # plain-text retry: succeeds
            None,  # next update: succeeds normally
        ]
    )
    reporter = MessageProgressReporter(message)

    await reporter.update(f"{texts.DOWNLOADING} 10%", is_terminal=False)

    assert not reporter._uneditable
    # First call used the normal (possibly-entity-bearing) text; the retry
    # dropped formatting entirely via parse_mode=None.
    assert message.edit.call_args_list[0].args == (f"{texts.DOWNLOADING} 10%",)
    assert message.edit.call_args_list[1].kwargs.get("parse_mode") is None
    assert message.edit.call_args_list[1].args == (f"{texts.DOWNLOADING} 10%",)

    # The reporter must still be usable for further progress - the whole
    # point of the fix is that one content error does not freeze the bar.
    await reporter.update(f"{texts.DOWNLOADING} 20%", is_terminal=False)
    assert reporter._last_text == f"{texts.DOWNLOADING} 20%"
    assert message.edit.call_count == 3


async def test_reply_markup_invalid_on_edit_retries_as_plain_text():
    message = MagicMock()
    message.edit = AsyncMock(side_effect=[ReplyMarkupInvalidError(None), None])
    reporter = MessageProgressReporter(message)

    await reporter.update(texts.DOWNLOAD_DONE, buttons=_CLEAR_BUTTONS)

    assert not reporter._uneditable
    assert reporter.terminal_delivered
    assert message.edit.call_count == 2
    # The retry drops the offending buttons entirely, not just the text formatting.
    assert "buttons" not in message.edit.call_args_list[1].kwargs


async def test_content_error_does_not_permanently_disable_non_terminal_updates():
    """The regression from production: after one EntityBoundsInvalidError,
    every subsequent non-terminal update must still attempt an edit rather
    than being dropped by the `_uneditable` short-circuit."""
    message = MagicMock()
    message.edit = AsyncMock(
        side_effect=[
            EntityBoundsInvalidError(None),
            None,  # plain-text retry succeeds
            None,  # next progress update
            None,  # upload progress update, simulating the log's later "Uploading" phase
        ]
    )
    reporter = MessageProgressReporter(message)

    await reporter.update(f"{texts.DOWNLOADING} 10%", is_terminal=False)
    await reporter.update(f"{texts.DOWNLOADING} 50%", is_terminal=False)
    await reporter.update(f"{texts.UPLOADING} 20%", is_terminal=False)

    assert message.edit.call_count == 4
    assert reporter._last_text == f"{texts.UPLOADING} 20%"


# =============================================================================
# B - the fallback (`respond`/new-message) path must never send the
# clear-buttons sentinel as `reply_markup`: it is only valid on `edit`.
# =============================================================================


async def test_terminal_fallback_respond_never_sends_clear_buttons_sentinel():
    """Reproduces the exact production failure: edit fails, and the
    fallback `respond()` used to forward `_CLEAR_BUTTONS` (an empty
    `ReplyInlineMarkup`), which Telegram rejects on a brand-new message
    with ReplyMarkupInvalidError. The summary must reach the user instead."""
    message = MagicMock()
    message.edit = AsyncMock(side_effect=ReplyMarkupInvalidError(None))
    new_message = MagicMock()
    message.respond = AsyncMock(return_value=new_message)
    message.delete = AsyncMock()

    reporter = MessageProgressReporter(message, clear_buttons_on_terminal=True)

    await reporter.update(texts.DOWNLOAD_DONE)

    assert reporter.terminal_delivered
    message.respond.assert_called_once()
    call = message.respond.call_args
    assert call.args == (texts.DOWNLOAD_DONE,)
    assert "buttons" not in call.kwargs
    # Delivered via the new message - the stale one is cleaned up, not left
    # behind with the frozen progress text.
    message.delete.assert_called_once()


async def test_terminal_fallback_respond_still_forwards_real_buttons():
    """The fix must only strip the *empty* clear-buttons sentinel - real
    buttons (e.g. a "צור קשר" button on a credits-exhausted message) still
    need to reach the fallback new message."""
    real_buttons = ReplyInlineMarkup([MagicMock()])
    message = MagicMock()
    message.edit = AsyncMock(side_effect=ReplyMarkupInvalidError(None))
    new_message = MagicMock()
    message.respond = AsyncMock(return_value=new_message)
    message.delete = AsyncMock()

    reporter = MessageProgressReporter(message)

    await reporter.update(texts.CREDITS_EXHAUSTED, buttons=real_buttons)

    message.respond.assert_called_once_with(texts.CREDITS_EXHAUSTED, buttons=real_buttons)
