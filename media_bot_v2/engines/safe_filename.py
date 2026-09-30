"""Filename safety shared by every route that turns an untrusted URL or
title into a path on disk: the direct-link engine, the provider downloader,
and the yt-dlp-backed engines (YouTube/TikTok/Instagram).

Two independent problems, fixed together because they share the same
call sites:

* Path traversal (finding A): a URL path segment like `..%2Fetc%2Fpasswd`
  must never be allowed to place a file outside the task's destination
  directory. Percent-decoding must happen *before* splitting on `/`, or an
  encoded separator survives basename extraction and turns into a real
  separator afterwards.
* Byte-length overflow (finding D): `NAME_MAX` on Linux is 255 *bytes*, not
  characters - a long Hebrew or emoji title can build a filename that looks
  short in Python's `len()` but is well over the limit once UTF-8 encoded,
  causing `OSError: [Errno 36] File name too long`.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|]')

# Safe target for a filename's total byte length: comfortably under the
# 255-byte NAME_MAX on ext4/most Linux filesystems, leaving room for an
# extension and any `[id]` suffix appended by a caller.
DEFAULT_MAX_NAME_BYTES = 200


def sanitize_component(text: str) -> str:
    """Strip characters that are unsafe or meaningless as a filename:
    control bytes, path/Windows-reserved punctuation, and leading dots or
    dashes (hidden files, or a name a shell could mistake for a flag)."""
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    cleaned = _UNSAFE_CHARS_RE.sub("_", cleaned)
    cleaned = cleaned.strip().strip(".").lstrip("-")
    return cleaned


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes UTF-8 bytes without splitting a
    multi-byte character in half."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def _split_ext(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0:  # no ext, or a leading dot (already stripped by sanitize_component anyway)
        return name, ""
    return name[:dot], name[dot:]


def safe_basename_from_url_path(
    raw: str,
    *,
    default_stem: str = "download",
    default_ext: str = "",
    max_bytes: int = DEFAULT_MAX_NAME_BYTES,
) -> str:
    """Turn an untrusted, possibly percent-encoded URL path (or a single
    path segment) into one safe filename component: decode first, take the
    last `/`-separated segment, strip anything traversal-shaped or unsafe,
    then truncate to max_bytes without breaking the extension or a UTF-8
    character."""
    decoded = unquote(raw).replace("\\", "/")
    last_segment = decoded.split("/")[-1]
    name = sanitize_component(last_segment)
    if not name:
        stem = sanitize_component(default_stem) or "download"
        return truncate_utf8_bytes(stem, max(max_bytes - len(default_ext.encode("utf-8")), 1)) + default_ext

    stem, ext = _split_ext(name)
    if not ext and default_ext:
        ext = default_ext
    budget = max(max_bytes - len(ext.encode("utf-8")), 1)
    stem = truncate_utf8_bytes(stem, budget)
    if not stem:
        stem = truncate_utf8_bytes(sanitize_component(default_stem) or "download", budget) or "f"
    return stem + ext


def build_safe_named_file(stem: str, *, suffix: str, max_bytes: int = DEFAULT_MAX_NAME_BYTES) -> str:
    """Combine a human-readable stem (e.g. a video title) with a fixed
    suffix (e.g. ` [<id>].mp4`) into one filename whose total byte length
    stays under max_bytes - only the stem is truncated, never the suffix,
    since it carries the uniqueness-bearing id and the real extension."""
    budget = max(max_bytes - len(suffix.encode("utf-8")), 1)
    cleaned_stem = truncate_utf8_bytes(sanitize_component(stem) or "file", budget)
    return (cleaned_stem or "file") + suffix


def within_directory(dest_dir: Path, filename: str, *, fallback: str) -> Path:
    """Resolve dest_dir/filename and verify the result is still inside
    dest_dir. Sanitized filenames never contain a separator, so this is a
    defense-in-depth backstop (e.g. against a symlink already present in
    dest_dir) rather than the primary defense - the primary defense is
    sanitize_component/safe_basename_from_url_path stripping separators and
    `..` before this is ever called."""
    base = dest_dir.resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base:
        candidate = (base / fallback).resolve()
        if candidate.parent != base:
            candidate = base / fallback
    return candidate
