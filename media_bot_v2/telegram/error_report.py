"""Error reports to the archive channel, restoring the old bot's
`report_error_to_archive` (media-downloader-bot/src/main.py:112-167).

`ErrorReporter.report()` is called from every relevant failure branch in
`telegram/router.py`, *after* the user has already seen the terminal failure
message - it never blocks or changes that path. It is synchronous and cheap:
it snapshots the current request's log buffer (`logging_setup.current_request_log`),
decides dedup/rate-limiting, and schedules the actual Telegram send as a
background task. A failed send is only ever a WARNING; it never raises back
into the caller.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests
from telethon import TelegramClient

from media_bot_v2.engines.base import DownloadTooLargeError, NotMediaContentError
from media_bot_v2.logging_setup import current_request_log
from media_bot_v2.providers.base import ProviderFetchError, mask_secrets
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.captions import MESSAGE_LIMIT, utf16_units
from media_bot_v2.telegram.flood_wait import FLOOD_WAIT_ERRORS, call_with_flood_retry

logger = logging.getLogger(__name__)

# A single flood wait is bounded the same way the quality-menu/button path
# bounds it (docs/LESSONS.md 10.8) - a report is a background nicety, never
# worth holding anything up for, let alone the main upload/download budget.
_REPORT_FLOOD_MAX_WAIT_SECONDS = 30.0
_REPORT_FLOOD_MAX_TOTAL_WAIT_SECONDS = 60.0

# Telegram's real message limit (4096 UTF-16 units) minus headroom for the
# header/body above the log section and for HTML-escape expansion of the
# log's tail - see build_report_text's docstring.
_TARGET_BUDGET = 3900
_MIN_LOG_BUDGET = 500

_TRUNCATED_USER_FIELD_LIMIT = 300
_URL_FIELD_LIMIT = 300

# engines/direct.py does not yet classify requests.HTTPError into a domain
# exception (that mapping is a later stage) - it currently reaches here as a
# bare requests.exceptions.HTTPError, so this is the one place that can give
# it the `http_4xx` kind the triage's ERROR_REPORT_SKIP list documents.
_HTTP_4XX_STATUSES = frozenset({401, 403, 404, 410, 451})


def classify_kind(exc: BaseException | None) -> str:
    """Maps an exception to the short, English `kind` slug used for dedup
    and as the report's `#kind` hashtag. Pure and total: every exception
    (including a bare `Exception`) resolves to some kind, defaulting to
    "internal"."""
    if exc is None:
        return "internal"
    if isinstance(exc, NotMediaContentError):
        return "not_media"
    if isinstance(exc, DownloadTooLargeError):
        return "too_large"
    if isinstance(exc, FLOOD_WAIT_ERRORS):
        return "flood"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, requests.exceptions.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in _HTTP_4XX_STATUSES:
            return "http_4xx"
        return "provider"
    if isinstance(exc, ProviderFetchError):
        return "provider"
    return "internal"


def _exc_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    # YouTubeDownloadError keeps the original (pre-classification) error text
    # here - more useful to an operator than the Hebrew user-facing message.
    original = getattr(exc, "original_error", None)
    return str(original) if original else str(exc)


def _truncate_plain(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _fit_lines_to_budget(lines: list[str], budget: int) -> tuple[list[str], int]:
    """Coarse pass: keep as many trailing (raw, pre-escape) lines as fit
    within `budget` characters - the tail, not the head, since the lines that
    explain a failure are the last ones written."""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        if total > budget and kept:
            break
        kept.insert(0, line)
    dropped = len(lines) - len(kept)
    return kept, dropped


def build_report_text(
    *,
    platform: str,
    kind: str,
    user_id: int,
    user_display: str,
    quality: str | None,
    url: str,
    user_text: str,
    exc: BaseException | None,
    elapsed_seconds: float,
    timestamp: str,
    log_lines: list[str],
    rate_limited_skipped: int = 0,
    attachment_available: bool = True,
) -> tuple[str, bool]:
    """Pure: builds the HTML report message. Returns `(text, log_truncated)`.

    Telegram's limit is 4096 UTF-16 units of *decoded* text
    (`captions.utf16_units`), not raw characters. The raw log is truncated
    (keeping its tail) before `html.escape`, since cutting already-escaped
    text could split an entity like `&amp;` and produce invalid HTML. A
    coarse character budget gets close in one pass; a line-at-a-time
    correction loop then accounts for HTML-escape expansion or wide
    (astral-plane) characters that the coarse pass cannot predict exactly.
    """
    platform_he = texts.ERROR_REPORT_PLATFORM_HE.get(platform, platform)
    quality_label = quality or texts.ERROR_REPORT_QUALITY_NONE
    exc_type = type(exc).__name__ if exc is not None else "Unknown"
    exc_text = _truncate_plain(mask_secrets(_exc_text(exc)), _TRUNCATED_USER_FIELD_LIMIT)
    user_text_plain = _truncate_plain(mask_secrets(user_text or ""), _TRUNCATED_USER_FIELD_LIMIT)
    # A url can carry a query string with its own secrets (?token=.../
    # ?api_key=...) and, separately, can be long enough on its own (long
    # tracking params etc.) to push the whole report past Telegram's 4096
    # limit - mask it like every other dynamic field, then cap its length
    # the same way exc_text/user_text are capped above.
    url_plain = _truncate_plain(mask_secrets(url or ""), _URL_FIELD_LIMIT)

    header = texts.ERROR_REPORT_HEADER.format(platform=html.escape(platform), kind=html.escape(kind))
    body = texts.ERROR_REPORT_BODY.format(
        user_display=html.escape(user_display or str(user_id)),
        user_id=user_id,
        platform_he=html.escape(platform_he),
        quality=html.escape(quality_label),
        url=html.escape(url_plain),
        user_text=html.escape(user_text_plain),
        exc_type=html.escape(exc_type),
        exc_text=html.escape(exc_text),
        elapsed=f"{elapsed_seconds:.1f}s",
        timestamp=timestamp,
    )
    rate_notice = ""
    if rate_limited_skipped > 0:
        rate_notice = texts.ERROR_REPORT_RATE_LIMITED_NOTICE.format(n=rate_limited_skipped) + "\n"
    header_body = f"{header}\n{body}\n{rate_notice}".rstrip("\n")

    # Defensive floor: every dynamic field above is already capped, but
    # never trust that alone - if the header on its own is still too close
    # to (or over) the limit, there is no `kept`/log-section loop below that
    # could ever bring the final message back under 4096, and the report
    # would be silently lost (Telegram rejects it, _send() only logs a
    # WARNING). Hard-clamp it here so build_report_text always returns
    # something Telegram will actually accept.
    while utf16_units(header_body) > MESSAGE_LIMIT - _MIN_LOG_BUDGET and len(header_body) > 100:
        header_body = header_body[: len(header_body) - 200]

    if not log_lines:
        log_section = texts.ERROR_REPORT_LOG_SECTION.format(log_tail=texts.ERROR_REPORT_NO_LOG)
        full_text = f"{header_body}\n{log_section}"
        while utf16_units(full_text) > MESSAGE_LIMIT and len(header_body) > 100:
            header_body = header_body[: len(header_body) - 200]
            full_text = f"{header_body}\n{log_section}"
        return full_text, False

    header_units = utf16_units(header_body)
    budget = max(_MIN_LOG_BUDGET, _TARGET_BUDGET - header_units)
    kept, dropped = _fit_lines_to_budget(log_lines, budget)

    def render(kept_lines: list[str], dropped_count: int) -> str:
        text = "\n".join(kept_lines)
        if dropped_count:
            notice_template = (
                texts.ERROR_REPORT_TRUNCATED_NOTICE
                if attachment_available
                else texts.ERROR_REPORT_TRUNCATED_NOTICE_NO_ATTACHMENT
            )
            notice = notice_template.format(n=dropped_count)
            text = f"{notice}\n{text}" if text else notice
        log_section = texts.ERROR_REPORT_LOG_SECTION.format(log_tail=html.escape(text))
        return f"{header_body}\n{log_section}"

    full_text = render(kept, dropped)
    while utf16_units(full_text) > MESSAGE_LIMIT and kept:
        dropped += 1
        kept = kept[1:]
        full_text = render(kept, dropped)

    # Last resort: even with every log line dropped, a header built entirely
    # from maximally-sized, escape-heavy fields could still be over budget on
    # its own - the loop above cannot fix that (it only ever shrinks `kept`,
    # which is already empty). Never return something over the limit.
    while utf16_units(full_text) > MESSAGE_LIMIT and len(header_body) > 100:
        header_body = header_body[: len(header_body) - 200]
        full_text = render(kept, dropped)

    return full_text, dropped > 0


class ErrorReporter:
    """Sends a Hebrew error report (text, plus a `.txt` attachment of the
    full log when the message had to be truncated) to the configured
    channel. Never touches credits, the retry store, or the pipeline, and
    never raises - see docs/LESSONS.md and the module docstring above."""

    def __init__(
        self,
        client: TelegramClient,
        channel: int | str | None,
        *,
        enabled: bool = True,
        skip_kinds: frozenset[str] = frozenset(),
        dedup_seconds: float = 600.0,
        max_per_minute: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._channel = channel
        self._enabled = enabled
        self._skip_kinds = skip_kinds
        self._dedup_seconds = dedup_seconds
        self._max_per_minute = max_per_minute
        self._clock = clock
        self._dedup: dict[tuple[int, str, str], float] = {}
        self._sent_at: deque[float] = deque()
        self._rate_limited_skipped = 0
        self._tasks: set[asyncio.Task] = set()

    def report(
        self,
        *,
        user_id: int,
        user_display: str,
        chat_id: int,
        url: str,
        platform: str,
        quality: str | None,
        kind: str,
        user_text: str,
        exc: BaseException | None,
        started_at: float,
    ) -> None:
        """Decide whether to report (enabled, not a duplicate, under the rate
        limit, not the archive channel itself) and - if so - schedule the
        actual send as a background task. Synchronous and cheap: never
        awaits, never touches the pipeline/credits/retry store."""
        if not self._enabled or not self._channel:
            return
        if chat_id == self._channel:
            logger.warning("Error report skipped: chat_id equals the configured channel (%r)", chat_id)
            return
        if kind in self._skip_kinds:
            logger.info("Error report skipped (kind=%s is in ERROR_REPORT_SKIP)", kind)
            return

        now = self._clock()
        dedup_key = (user_id, url, kind)
        self._prune_dedup(now)
        last = self._dedup.get(dedup_key)
        if last is not None and (now - last) < self._dedup_seconds:
            logger.info("Error report skipped (dedup: user=%s url=%s kind=%s)", user_id, url, kind)
            return

        if not self._try_consume_rate_slot(now):
            logger.info("Error report skipped (rate limit: %d/min reached)", self._max_per_minute)
            return

        self._dedup[dedup_key] = now
        rate_limited_skipped = self._rate_limited_skipped
        self._rate_limited_skipped = 0

        # Snapshotted now, before the task is even created, so the reporter's
        # own log lines (this method logs nothing at report()-call time
        # beyond what's above) never end up inside the report they describe.
        log_text = current_request_log()
        elapsed_seconds = max(0.0, time.monotonic() - started_at)
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S %d.%m")

        task = asyncio.create_task(
            self._send(
                user_id=user_id,
                user_display=user_display,
                url=url,
                platform=platform,
                quality=quality,
                kind=kind,
                user_text=user_text,
                exc=exc,
                elapsed_seconds=elapsed_seconds,
                timestamp=timestamp,
                log_text=log_text,
                rate_limited_skipped=rate_limited_skipped,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _prune_dedup(self, now: float) -> None:
        stale = [key for key, at in self._dedup.items() if now - at >= self._dedup_seconds]
        for key in stale:
            del self._dedup[key]

    def _prune_rate_window(self, now: float) -> None:
        while self._sent_at and now - self._sent_at[0] >= 60.0:
            self._sent_at.popleft()

    def _rate_slot_available(self, now: float) -> bool:
        """Peeks whether a rate slot is currently free, without consuming
        one - used to decide, before the message text is built, whether the
        attachment will actually be sendable, so the message never promises
        an attachment the budget is about to block."""
        self._prune_rate_window(now)
        return len(self._sent_at) < self._max_per_minute

    def _try_consume_rate_slot(self, now: float, *, count_as_skip: bool = True) -> bool:
        """Spends one slot of the global per-minute token bucket - used both
        for the report's message and, separately, for its attached log file
        (triage 3.6: "הודעה וקובץ נספרים כל אחד בנפרד"), so a stream of
        truncated reports cannot double the real send rate to the channel
        past `ERROR_REPORT_MAX_PER_MINUTE`.

        `count_as_skip` gates whether a failure bumps `_rate_limited_skipped`
        - the counter behind the "N reports skipped" notice must only count
        reports that were themselves skipped (the `report()` call site below),
        never an attachment that was skipped while its report still went out
        (the `_send_unsafe` call site), or the notice would claim more drops
        than actually happened.
        """
        self._prune_rate_window(now)
        if len(self._sent_at) >= self._max_per_minute:
            if count_as_skip:
                self._rate_limited_skipped += 1
            return False
        self._sent_at.append(now)
        return True

    async def _send(self, **kwargs: Any) -> None:
        try:
            await self._send_unsafe(**kwargs)
        except Exception:
            logger.warning("Error report to archive failed", exc_info=True)

    async def _send_unsafe(
        self,
        *,
        user_id: int,
        user_display: str,
        url: str,
        platform: str,
        quality: str | None,
        kind: str,
        user_text: str,
        exc: BaseException | None,
        elapsed_seconds: float,
        timestamp: str,
        log_text: str,
        rate_limited_skipped: int,
    ) -> None:
        log_lines = log_text.split("\n") if log_text else []
        # Peeked (not consumed) before the text is built, so the message
        # never promises an attachment that the budget is about to block -
        # see _rate_slot_available's docstring.
        attachment_available = self._rate_slot_available(self._clock())
        text, truncated = build_report_text(
            platform=platform,
            kind=kind,
            user_id=user_id,
            user_display=user_display,
            quality=quality,
            url=url,
            user_text=user_text,
            exc=exc,
            elapsed_seconds=elapsed_seconds,
            timestamp=timestamp,
            log_lines=log_lines,
            rate_limited_skipped=rate_limited_skipped,
            attachment_available=attachment_available,
        )
        # A new message, never an edit - no `buttons`/`reply_markup` kwarg at
        # all (docs/LESSONS.md 11: an empty ReplyInlineMarkup([]) is rejected
        # by Telegram, and this message never needs a keyboard in the first
        # place). link_preview=False: the channel carries links to adult-
        # content sites among others.
        sent = await call_with_flood_retry(
            self._client.send_message,
            self._channel,
            text,
            parse_mode="html",
            link_preview=False,
            max_wait_seconds=_REPORT_FLOOD_MAX_WAIT_SECONDS,
            max_total_wait_seconds=_REPORT_FLOOD_MAX_TOTAL_WAIT_SECONDS,
        )
        if truncated and log_text:
            # The attachment is its own send against the channel, counted
            # separately from the message above (triage 3.6) - skip it
            # rather than let a burst of truncated reports double the real
            # per-minute send rate past ERROR_REPORT_MAX_PER_MINUTE.
            if not attachment_available or not self._try_consume_rate_slot(
                self._clock(), count_as_skip=False
            ):
                logger.info("Error report log attachment skipped (rate limit: %d/min reached)", self._max_per_minute)
                return
            full_log = mask_secrets(log_text)
            now_local = datetime.now().astimezone()
            file_name = f"error_log_{user_id}_{now_local.strftime('%Y%m%d-%H%M%S')}.txt"
            # Telethon's send_file has no `file_name` parameter - it has none
            # at all and a stray `file_name=` kwarg is silently swallowed, so
            # the attachment arrived as "unnamed". Telethon derives the name
            # from the file-like object's own `.name` attribute instead
            # (telethon.utils.get_attributes), so set it directly on the buffer.
            buf = io.BytesIO(full_log.encode("utf-8"))
            buf.name = file_name
            await call_with_flood_retry(
                self._client.send_file,
                self._channel,
                buf,
                caption=texts.ERROR_REPORT_FILE_CAPTION.format(user_id=user_id),
                parse_mode="html",
                reply_to=getattr(sent, "id", None),
                max_wait_seconds=_REPORT_FLOOD_MAX_WAIT_SECONDS,
                max_total_wait_seconds=_REPORT_FLOOD_MAX_TOTAL_WAIT_SECONDS,
            )
