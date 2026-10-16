"""Base interface for external extraction providers (TikWM, musicaldown, etc.).

Providers fetch direct media URLs and metadata without downloading full media
files to disk. The pipeline then streams the resulting direct URL(s) to disk.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# Query-string secrets (api_key=..., token=..., ...), JSON/text body secrets
# ("key": "value"), and bearer tokens that show up verbatim inside provider
# exception messages - `requests` embeds the full request URL in
# connection/HTTP errors, so "ytmp3 auth request failed: <requests
# exception>" leaked the ytmp3 API key into logs and the
# provider_health.last_error column at WARNING level (production incident
# 2026-10-01). Anything matching one of these keys gets masked before the
# text reaches a log line or is persisted anywhere.
#
# The key must be preceded by a non-identifier character, the start of the
# string, or a 2-digit percent-escape (`%3F` etc.) - not a bare `\b` - so a
# fully percent-encoded nested query string (`...%3Fapi_key%3Dsecret123`,
# where even the `?` got encoded and no literal separator character exists
# between the escape and the key) still counts as a boundary. This is also
# what keeps "tokenizer"/"keyboard"/a "turkey=5"-style innocent param from
# being touched: the separator must follow the key immediately (after
# optional quote/whitespace), so "izer"/"board" right after the key simply
# never matches that separator.
_SENSITIVE_KEYS = (
    r"x[-_]api[-_]key|api[-_]?key|apikey|"
    r"access[-_]?token|refresh[-_]?token|"
    r"authorization|auth|"
    r"password|passwd|pwd|"
    r"signature|sig|secret|token|key"
)
_SENSITIVE_QUERY_PARAM_RE = re.compile(
    r"(?i)(?:^|(?<=[^A-Za-z0-9_])|(?<=%[0-9A-Fa-f]{2}))"
    r"(" + _SENSITIVE_KEYS + r")"
    r"[\"']?\s*(?:=|%3[dD]|:)\s*"
    r"(?!bearer\b)"  # leave "Authorization: Bearer <token>" to _BEARER_TOKEN_RE below
    r"(\"[^\"]*\"|'[^']*'|[^&\s\"'<>,}]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")

# `Cookie: SID=...; session=...` / `Set-Cookie: ...` headers: the value after
# `SID=` is not caught by _SENSITIVE_QUERY_PARAM_RE above because "SID" and
# arbitrary cookie names are not in the sensitive-key list - there is no
# bounded set of cookie names to enumerate, so the whole header value is
# redacted instead of trying to parse individual pairs. The header name may
# be followed by a closing quote before the colon (a Python dict repr like
# `'Cookie': 'SID=...'` or a JSON body like `"Cookie": "SID=..."`), so an
# optional quote plus whitespace is allowed on both sides of the colon; the
# value itself is then terminated at the matching closing quote when one is
# present, or at end of line otherwise - never past it, so a dict repr's
# next key is left untouched.
_COOKIE_HEADER_RE = re.compile(
    r"(?i)\b(cookie|set-cookie)[\"']?\s*:\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]*)"
)

# A bare Telegram bot token (`123456789:AAH...`), as opposed to one embedded
# in a `token=`/`api_key=`-style query param (already covered above) - this
# is the exact shape that leaked into the journal in production before
# M11.17 masked it in the provider path only, not generally. The token's own
# digit run is immediately preceded by a letter in a Bot API URL
# (`.../bot123456789:AAH.../sendMessage`), and a letter-to-digit transition is
# not a `\b` word boundary, so the boundary is a negative lookbehind for
# another digit instead - that still rejects matching a few digits in the
# middle of a longer run, while letting a preceding letter through.
_BOT_TOKEN_RE = re.compile(r"(?<!\d)(\d{6,10}):[A-Za-z0-9_-]{30,}\b")


def mask_secrets(text: str) -> str:
    """Redact query-string secrets, JSON/text-body secrets, bearer tokens,
    cookie headers, and bare bot tokens from free-form text (an exception
    message, a log line, ...) before it is logged or stored. Safe to call on
    text with nothing to mask - returns it unchanged."""
    if not text:
        return text
    masked = _SENSITIVE_QUERY_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    masked = _BEARER_TOKEN_RE.sub("Bearer ***", masked)
    masked = _COOKIE_HEADER_RE.sub(lambda m: f"{m.group(1)}: ***", masked)
    masked = _BOT_TOKEN_RE.sub(lambda m: f"{m.group(1)}:***", masked)
    return masked


class ProviderError(Exception):
    """Base exception for provider failures."""


class ProviderFetchError(ProviderError):
    """Raised when a provider fails to extract direct media URLs."""


class ProviderUnavailableError(ProviderFetchError):
    """Raised when a provider is disabled or not configured (e.g. missing cobalt URL)."""


@dataclass
class ProviderResult:
    """Extraction result containing direct media URL(s) and metadata."""

    provider: str
    media_urls: list[str]
    title: str | None = None
    media_type: str = "video"  # "video", "audio", "photo"
    size_hint: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_url(self) -> str:
        if not self.media_urls:
            raise ValueError("ProviderResult contains no media URLs")
        return self.media_urls[0]


class BaseProvider(ABC):
    """Contract for external media extraction providers."""

    name: str
    supported_platforms: tuple[str, ...]

    def __init__(self, *, timeout: float = 15.0) -> None:
        self.timeout = timeout

    @abstractmethod
    def matches(self, url: str) -> bool:
        """Return True if this provider can handle the given URL."""

    @abstractmethod
    async def fetch(self, url: str) -> ProviderResult:
        """Extract direct media URL(s) and metadata.

        Runs network requests in worker threads via the dedicated thread pool
        (media_bot_v2.executor.run_in_thread) to avoid blocking the event loop.
        """
