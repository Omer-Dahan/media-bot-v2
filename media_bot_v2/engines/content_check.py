"""Non-media content detection shared by the direct engine and the provider downloader.

Two independent signals, because either alone is easy to defeat: servers send
error pages with `Content-Type: application/octet-stream`, and send JSON or
plain text with a media-looking URL. A response is rejected if its declared
Content-Type is a text/document type, or if the first bytes of the body look
like markup or JSON.
"""

from __future__ import annotations

import codecs

from media_bot_v2.engines.base import NotMediaContentError

SNIFF_BYTES = 1024

_REJECTED_TYPES = frozenset(
    {
        "application/xhtml+xml",
        "application/json",
        "application/xml",
        "application/javascript",
        "application/ld+json",
    }
)
_MARKUP_PREFIXES = (
    "<!doctype",
    "<html",
    "<head",
    "<body",
    "<?xml",
    "<!--",
    "<script",
    "<meta",
    "<title",
    "<div",
    "<p>",
    "<rss",
    "<error",
)
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _header_content_type(response) -> str:
    value = response.headers.get("Content-Type", "")
    if not isinstance(value, str):
        return ""
    return value.split(";")[0].strip().lower()


def reject_if_not_media_content_type(response) -> None:
    content_type = _header_content_type(response)
    if content_type.startswith("text/") or content_type in _REJECTED_TYPES or content_type.endswith(("+json", "+xml")):
        raise NotMediaContentError()


def _decode_head(head: bytes) -> str | None:
    """Decode the first bytes as text, honouring a BOM. None if it is not text."""
    encoding = "utf-8"
    for bom, name in _BOMS:
        if head.startswith(bom):
            head = head[len(bom):]
            encoding = name
            break
    else:
        if b"\x00" in head:
            return None
    try:
        return codecs.getincrementaldecoder(encoding)(errors="strict").decode(head, final=False)
    except UnicodeDecodeError:
        return None


def looks_like_non_media(head: bytes) -> bool:
    text = _decode_head(head)
    if text is None:
        return False
    stripped = text.lstrip().lower()
    if not stripped:
        return False
    if stripped.startswith(_MARKUP_PREFIXES):
        return True
    # A JSON body. Real media containers never open with a bare '{' or '['
    # followed by JSON-ish text, and it must decode cleanly as text to get here.
    return stripped[0] in "{[" and all(ch.isprintable() or ch in "\r\n\t" for ch in stripped[:256])


def reject_if_not_media_body(head: bytes) -> None:
    if looks_like_non_media(head):
        raise NotMediaContentError()
