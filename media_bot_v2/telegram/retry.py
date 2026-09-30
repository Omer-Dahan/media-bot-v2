"""M11.5: Retry button state store and helpers.

Provides a TTL-bounded, size-capped in-memory store for retryable requests.
When a download fails on a retryable error, the failure message is equipped
with a 🔄 "נסה שוב" button. Clicking it re-executes the exact same request
through the same pipeline.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from telethon import Button

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.engines.base import DownloadTooLargeError, UnsupportedUrlError
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.callback_data import encode

HOPELESS_EXCEPTIONS = (
    UnsupportedUrlError,
    DownloadTooLargeError,
    UserBlockedException,
    CreditsExhaustedException,
    BandwidthExhaustedException,
)

HOPELESS_TEXTS = (
    texts.UNSUPPORTED_URL,
    texts.NOT_MEDIA_CONTENT,
    texts.INSTAGRAM_PRIVATE_OR_LOGIN,
    texts.INSTAGRAM_NOT_FOUND,
    texts.INSTAGRAM_UNSUPPORTED_MEDIA,
    texts.CREDITS_EXHAUSTED,
    texts.BANDWIDTH_EXHAUSTED,
    texts.REQUEST_CANCELLED,
    "המשתמש שלך נחסם",
    "נחסם",
    "הגבלה גיאוגרפית",
    "פרטי, נמחק",
    "הפלייליסט אינו זמין",
    # Server-configuration failures (missing JS runtime, PO token, invalid
    # cookies): only the operator can fix these, and retrying the exact same
    # request just repeats the exact same failure - no retry button.
    "חסר בשרת runtime של JavaScript",
    "דורש PO token",
    "קובץ ה-cookies אינו תקין",
    # Requested quality/format unavailable for this video: retrying at the
    # same quality will fail again - the message already points the user at
    # the quality menu instead.
    "הפורמט המבוקש אינו זמין",
)


def is_hopeless_failure(exc: Exception | None = None, text: str | None = None) -> bool:
    """Return True if the failure is hopeless (no point in retrying) so that
    no retry button should be attached."""
    if exc is not None:
        if isinstance(exc, HOPELESS_EXCEPTIONS):
            return True
        msg = str(exc)
        if any(h in msg for h in HOPELESS_TEXTS):
            return True
        lowered_exc = msg.lower()
        if "unsupported" in lowered_exc or "too large" in lowered_exc or "not supported" in lowered_exc:
            return True
        if "סרטון פרטי" in msg or "אינו זמין (סרטון פרטי)" in msg or "נחסם" in msg:
            return True

    if text is not None:
        if any(h in text for h in HOPELESS_TEXTS):
            return True
        if text.startswith((texts.DOWNLOAD_DONE, "הושלם")):
            return True
        lowered = text.lower()
        if "unsupported" in lowered or "too large" in lowered or "not supported" in lowered:
            return True
        if "גדול מדי" in text or "אינו נתמך" in text:
            return True
        if "סרטון פרטי" in text or "אינו זמין (סרטון פרטי)" in text or "נחסם" in text:
            return True

    return False


@dataclass
class RetryContext:
    retry_id: str
    user_id: int
    chat_id: int
    message_id: int
    url: str
    platform: str  # "youtube", "tiktok", "instagram", "direct"
    quality: str | None = None
    created_at: float = 0.0
    is_running: bool = False


class RetryStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 2000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, RetryContext] = OrderedDict()
        self._by_msg: dict[tuple[int, int], str] = {}

    def put(
        self,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        url: str,
        platform: str,
        quality: str | None = None,
    ) -> RetryContext:
        self._evict_expired()
        msg_key = (chat_id, message_id)
        old_id = self._by_msg.pop(msg_key, None)
        if old_id is not None:
            self._entries.pop(old_id, None)

        retry_id = uuid.uuid4().hex[:10]
        ctx = RetryContext(
            retry_id=retry_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            url=url,
            platform=platform,
            quality=quality,
            created_at=self._clock(),
            is_running=False,
        )
        self._entries[retry_id] = ctx
        self._by_msg[msg_key] = retry_id

        while len(self._entries) > self._max_entries:
            _, oldest_ctx = self._entries.popitem(last=False)
            self._by_msg.pop((oldest_ctx.chat_id, oldest_ctx.message_id), None)

        return ctx

    def get(self, key: str | tuple[int, int]) -> RetryContext | None:
        self._evict_expired()
        if isinstance(key, tuple):
            retry_id = self._by_msg.get(key)
            if retry_id is None:
                return None
            return self._entries.get(retry_id)
        return self._entries.get(key)

    def remove(self, key: str | tuple[int, int]) -> RetryContext | None:
        self._evict_expired()
        if isinstance(key, tuple):
            retry_id = self._by_msg.pop(key, None)
            if retry_id is None:
                return None
            return self._entries.pop(retry_id, None)
        ctx = self._entries.pop(key, None)
        if ctx is not None:
            msg_key = (ctx.chat_id, ctx.message_id)
            if self._by_msg.get(msg_key) == ctx.retry_id:
                self._by_msg.pop(msg_key, None)
        return ctx

    def clear(self) -> None:
        self._entries.clear()
        self._by_msg.clear()

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._entries)

    def _evict_expired(self, now: float | None = None) -> None:
        if now is None:
            now = self._clock()
        expired = [
            retry_id
            for retry_id, ctx in self._entries.items()
            if now - ctx.created_at > self._ttl
        ]
        for retry_id in expired:
            ctx = self._entries.pop(retry_id, None)
            if ctx is not None:
                msg_key = (ctx.chat_id, ctx.message_id)
                if self._by_msg.get(msg_key) == ctx.retry_id:
                    self._by_msg.pop(msg_key, None)


def build_retry_markup(retry_id: str) -> list[list[Button]]:
    return [[Button.inline(texts.RETRY_BUTTON, encode("retry", retry_id))]]
