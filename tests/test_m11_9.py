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

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import EntityBoundsInvalidError, ReplyMarkupInvalidError
from telethon.extensions import markdown
from telethon.tl.types import ReplyInlineMarkup

from media_bot_v2.telegram import texts
from media_bot_v2.telegram.progress import MessageProgressReporter
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
    real_buttons = ReplyInlineMarkup([MagicMock()])

    await reporter.update(texts.DOWNLOAD_DONE, buttons=real_buttons)

    assert not reporter._uneditable
    assert reporter.terminal_delivered
    assert message.edit.call_count == 2
    # The retry drops the offending buttons entirely - and does so via an
    # *explicit* `buttons=None`, not by omitting the kwarg: Telethon's real
    # `Message.edit` only overrides `reply_markup` when `buttons` is present
    # in kwargs at all, so omitting it here would silently re-attach
    # whatever `reply_markup` the message already has instead of clearing it.
    retry_kwargs = message.edit.call_args_list[1].kwargs
    assert "buttons" in retry_kwargs
    assert retry_kwargs["buttons"] is None


class _RealisticEditMessage:
    """A fake `edit` that reproduces Telethon's real kwarg-presence rule
    (see `telethon/tl/custom/message.py`'s `_edit`/`edit`): if `buttons` is
    *absent* from kwargs, the existing `reply_markup` is reused untouched;
    if `buttons=None` is *present*, `reply_markup` is actually cleared. It
    also reproduces live Telegram's real rejection of an empty
    `ReplyInlineMarkup([])` as `reply_markup` (`ReplyMarkupInvalidError`).

    Unlike a fake that only raises on the exact sentinel the production code
    happens to build today (which can never fail, since it just checks that
    the code does what the code does), this one tracks the actual resulting
    keyboard state after the edit - so it fails if `update()` ever regresses
    to a plain `edit(text)` call (no `buttons` kwarg) where a clear was
    intended, exactly like the M11.15->M11.16 regression this round fixes."""

    def __init__(self, reply_markup: Any) -> None:
        self.reply_markup = reply_markup
        self.edit_calls: list[dict] = []

    async def edit(self, text: str, **kwargs: Any) -> None:
        self.edit_calls.append(kwargs)
        new_markup = kwargs.get("buttons", self.reply_markup)
        if isinstance(new_markup, ReplyInlineMarkup) and not new_markup.rows:
            raise ReplyMarkupInvalidError(None)
        self.reply_markup = new_markup


async def test_terminal_clear_actually_removes_the_inline_keyboard():
    """Replacement for the old (tautological) sentinel test: that test built
    a fake `edit` that raised only on the precise empty-`ReplyInlineMarkup`
    object the production code happened to construct at the time, so it
    verified the code matched itself, not real Telegram/Telethon behavior -
    it could never fail. This test instead drives `_RealisticEditMessage`,
    which reproduces Telethon's actual "buttons must be present in kwargs to
    override reply_markup" rule and Telegram's actual "empty ReplyInlineMarkup
    is rejected" rule.

    Proof this catches the original bug: reverting `update()`'s terminal
    clear branch from `edit(text, buttons=None)` back to a plain `edit(text)`
    (what M11.15 shipped) makes `_RealisticEditMessage.edit` take the
    "buttons absent" branch, which reuses `self.reply_markup` unchanged - so
    the final `message.reply_markup is None` assertion below would fail and
    `existing_button` would still be attached after a supposedly-terminal,
    button-clearing update."""
    existing_button = ReplyInlineMarkup([MagicMock()])
    message = _RealisticEditMessage(reply_markup=existing_button)
    reporter = MessageProgressReporter(message, clear_buttons_on_terminal=True)

    await reporter.update(texts.DOWNLOAD_DONE)

    assert reporter.terminal_delivered
    assert len(message.edit_calls) == 1
    assert "buttons" in message.edit_calls[0]
    assert message.edit_calls[0]["buttons"] is None
    assert message.reply_markup is None


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
# B - the fallback (`respond`/new-message) path must never send a
# clear-buttons `None` as `reply_markup`: a brand-new message has no
# existing keyboard to clear, and the resulting bare `buttons=None` kwarg
# would needlessly be forwarded to `respond()` where it isn't needed.
# =============================================================================


async def test_terminal_fallback_respond_never_sends_clear_buttons_sentinel():
    """Edit fails (content/markup rejected), falling back to `respond()` for
    a terminal message whose buttons were meant to be cleared (`buttons`
    stays `None` throughout - there's nothing to forward to a new message).
    The summary must still reach the user."""
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
