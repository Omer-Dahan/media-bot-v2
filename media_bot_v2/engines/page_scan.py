"""Static-HTML media scanner (page_scan v1).

Scans a public web page's raw HTML for directly-fetchable video/audio URLs so
the router can offer the user an IDM-like picker instead of failing the page
with NotMediaContentError. No JS execution, no cookies/auth, no DRM (.mpd is
out of scope - see spec).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from media_bot_v2.engines.ssrf_guard import SSRFBlockedError, assert_safe_url, safe_request

logger = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mov", ".mkv"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus"}
_HLS_EXTS = {".m3u8"}
_ALL_EXTS = _VIDEO_EXTS | _AUDIO_EXTS | _HLS_EXTS
_DROP_EXTS = {".mpd", ".ts"}

_VOID_TAGS = {"meta", "base", "source", "img", "br", "hr", "link", "input"}

_OG_META_KEYS = {
    "og:video": "video",
    "og:video:url": "video",
    "og:video:secure_url": "video",
    "og:audio": "audio",
    "twitter:player:stream": "video",
}

_KIND_ORDER = {"video": 0, "audio": 1, "hls": 2}

_EXT_RE = re.compile(r"\.([A-Za-z0-9]{2,5})$")
_HEIGHT_RE = re.compile(r"(?<![0-9])(\d{3,4})p(?![0-9a-zA-Z])")
_URL_SWEEP_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp4|m4v|webm|mov|mkv|mp3|m4a|aac|ogg|opus|m3u8)"
    r"(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    kind: str
    ext: str
    height: int | None
    origin: str
    title: str | None = None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_href: str | None = None
        self.meta_candidates: list[tuple[str, str]] = []
        self.media_srcs: list[tuple[str, str]] = []
        self.anchors: list[str] = []
        self.ld_json_blobs: list[str] = []
        self._tag_stack: list[str] = []
        self._in_ld_json = False
        self._ld_json_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k.lower(): v for k, v in attrs if v is not None}
        if tag == "base" and self.base_href is None:
            href = attr_dict.get("href")
            if href:
                self.base_href = href
        elif tag == "meta":
            prop = attr_dict.get("property") or attr_dict.get("name")
            content = attr_dict.get("content")
            if prop and content:
                kind = _OG_META_KEYS.get(prop.lower())
                if kind:
                    self.meta_candidates.append((content, kind))
        elif tag in ("video", "audio"):
            src = attr_dict.get("src")
            if src:
                self.media_srcs.append((src, tag))
        elif tag == "source":
            src = attr_dict.get("src")
            if src:
                parent_kind = "video"
                for ancestor in reversed(self._tag_stack):
                    if ancestor in ("video", "audio"):
                        parent_kind = ancestor
                        break
                self.media_srcs.append((src, parent_kind))
        elif tag == "a":
            href = attr_dict.get("href")
            if href:
                self.anchors.append(href)
        elif tag == "script":
            script_type = (attr_dict.get("type") or "").lower()
            if script_type == "application/ld+json":
                self._in_ld_json = True
                self._ld_json_buf = []
        if tag not in _VOID_TAGS:
            self._tag_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld_json:
            self.ld_json_blobs.append("".join(self._ld_json_buf))
            self._in_ld_json = False
            self._ld_json_buf = []
        if tag in self._tag_stack:
            while self._tag_stack and self._tag_stack[-1] != tag:
                self._tag_stack.pop()
            if self._tag_stack:
                self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_json_buf.append(data)


def _clean_url_text(text: str) -> str:
    text = unescape(text)
    text = text.replace("\\/", "/")
    text = text.replace("\\u002F", "/").replace("\\u002f", "/")
    return text


def _strip_trailing_punct(url: str) -> str:
    return url.rstrip(",;)\"'")


def _sweep_urls(html_text: str) -> list[str]:
    text = html_text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    return [_strip_trailing_punct(unescape(m)) for m in _URL_SWEEP_RE.findall(text)]


def _walk_json_ld(
    node: object, found: list[tuple[str, int | None, str | None]]
) -> None:
    if isinstance(node, dict):
        url_value = None
        for key in ("contentUrl", "embedUrl"):
            value = node.get(key)
            if isinstance(value, str):
                url_value = value
                break
        if url_value:
            height_raw = node.get("height")
            height = int(height_raw) if isinstance(height_raw, (int, float)) else None
            name = node.get("name")
            title = name if isinstance(name, str) else None
            found.append((url_value, height, title))
        for value in node.values():
            _walk_json_ld(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_json_ld(item, found)


def _extension_of(path: str) -> str | None:
    match = _EXT_RE.search(path)
    if not match:
        return None
    return "." + match.group(1).lower()


def _kind_for(ext: str, kind_hint: str) -> str:
    if ext in _HLS_EXTS:
        return "hls"
    if ext in _AUDIO_EXTS or kind_hint == "audio":
        return "audio"
    return "video"


def _height_hint_from_url(url: str) -> int | None:
    match = _HEIGHT_RE.search(url)
    return int(match.group(1)) if match else None


def _sort_key(candidate: MediaCandidate) -> tuple:
    return (
        _KIND_ORDER.get(candidate.kind, 3),
        0 if candidate.height is not None else 1,
        -(candidate.height or 0),
        candidate.url,
    )


def _extract_charset(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


def _extract_candidates(html_text: str, page_url: str) -> list[MediaCandidate]:
    parser = _PageParser()
    try:
        parser.feed(html_text)
    except Exception as exc:  # noqa: BLE001 - malformed HTML must not abort the scan
        logger.debug("page_scan: HTML parse error for %s: %s", page_url, exc)

    base_url = page_url
    if parser.base_href:
        base_url = urljoin(page_url, _clean_url_text(parser.base_href))

    # (url_text, kind_hint, origin, height_hint, title)
    raw_candidates: list[tuple[str, str, str, int | None, str | None]] = []

    for content, kind in parser.meta_candidates:
        raw_candidates.append((content, kind, "og", None, None))

    for src, parent_kind in parser.media_srcs:
        raw_candidates.append((src, parent_kind, "media_tag", None, None))

    for blob in parser.ld_json_blobs:
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        found: list[tuple[str, int | None, str | None]] = []
        _walk_json_ld(data, found)
        for url_text, height, title in found:
            raw_candidates.append((url_text, "video", "json_ld", height, title))

    for url_text in _sweep_urls(html_text):
        raw_candidates.append((url_text, "video", "inline_json", None, None))

    for href in parser.anchors:
        raw_candidates.append((href, "video", "anchor", None, None))

    seen: dict[str, MediaCandidate] = {}
    for url_text, kind_hint, origin, height_hint, title in raw_candidates:
        cleaned = _clean_url_text(url_text)
        resolved = urljoin(base_url, cleaned)
        parsed = urlparse(resolved)
        if parsed.scheme not in ("http", "https"):
            continue

        ext = _extension_of(parsed.path)
        if ext is None or ext in _DROP_EXTS or ext not in _ALL_EXTS:
            continue

        normalized_key = resolved.split("#", 1)[0]
        if normalized_key in seen:
            continue

        height = height_hint if height_hint is not None else _height_hint_from_url(resolved)
        candidate = MediaCandidate(
            url=normalized_key,
            kind=_kind_for(ext, kind_hint),
            ext=ext,
            height=height,
            origin=origin,
            title=title,
        )
        try:
            assert_safe_url(candidate.url)
        except SSRFBlockedError:
            continue
        seen[normalized_key] = candidate

    candidates = list(seen.values())
    candidates.sort(key=_sort_key)
    return candidates


def scan_page(
    url: str,
    *,
    timeout: float = 15.0,
    max_bytes: int = 3 * 1024 * 1024,
    max_candidates: int = 20,
) -> list[MediaCandidate]:
    try:
        response = safe_request(
            "GET",
            url,
            timeout=(5, timeout),
            stream=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; media-bot/1.0)",
                "Accept": "text/html,*/*",
            },
        )
    except SSRFBlockedError:
        raise
    except Exception as exc:  # noqa: BLE001 - any recoverable network error means no candidates
        logger.debug("page_scan: request failed for %s: %s", url, exc)
        return []

    try:
        content_type = response.headers.get("Content-Type", "") or ""
        body = bytearray()
        try:
            for chunk in response.iter_content(64 * 1024):
                if chunk:
                    body.extend(chunk)
                if len(body) >= max_bytes:
                    break
        except Exception as exc:  # noqa: BLE001 - a dropped connection just means no candidates
            logger.debug("page_scan: read failed for %s: %s", url, exc)
            return []
    finally:
        response.close()

    if not body:
        return []

    body_bytes = bytes(body[:max_bytes])
    content_type_main = content_type.split(";")[0].strip().lower()
    is_html_ct = content_type_main in ("text/html", "application/xhtml+xml")
    if not is_html_ct:
        prefix = body_bytes[:200].lstrip().lower()
        if not prefix.startswith((b"<!doctype html", b"<html")):
            return []

    charset = _extract_charset(content_type)
    try:
        text = body_bytes.decode(charset or "utf-8", errors="replace")
    except LookupError:
        text = body_bytes.decode("utf-8", errors="replace")

    try:
        candidates = _extract_candidates(text, url)
    except Exception as exc:  # noqa: BLE001 - a parse bug must not crash the caller
        logger.debug("page_scan: candidate extraction failed for %s: %s", url, exc)
        return []

    return candidates[:max_candidates]


def describe_candidate(candidate: MediaCandidate) -> str:
    filename = candidate.url.rsplit("/", 1)[-1].split("?", 1)[0] or candidate.url
    if candidate.height:
        return f"{candidate.kind} {candidate.height}p · {filename}"
    return f"{candidate.kind} · {filename}"
