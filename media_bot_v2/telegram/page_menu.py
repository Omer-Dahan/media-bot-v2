"""Pure helpers for the page-scan inline picker (router.py owns all state).

No Telethon client calls here - see media_bot_v2/engines/page_scan.py for the
scanner itself and telegram/router.py for the handlers that wire this in.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

from telethon import Button

from media_bot_v2.engines.page_scan import MediaCandidate
from media_bot_v2.telegram import texts
from media_bot_v2.telegram.callback_data import decode, encode

_KIND_LABELS = {
    "video": texts.PAGE_SCAN_KIND_VIDEO,
    "audio": texts.PAGE_SCAN_KIND_AUDIO,
    "hls": texts.PAGE_SCAN_KIND_HLS,
}

_MAX_FILENAME_LEN = 48


@dataclass(frozen=True)
class PendingPageScan:
    url: str
    owner_id: int
    candidates: tuple[MediaCandidate, ...]
    created_at: float


def _filename_for(candidate: MediaCandidate) -> str:
    parsed = urlparse(candidate.url)
    name = parsed.path.rsplit("/", 1)[-1]
    if not name:
        name = parsed.netloc or candidate.url
    if len(name) > _MAX_FILENAME_LEN:
        name = name[: _MAX_FILENAME_LEN - 1] + "…"
    return name


def button_label(index: int, candidate: MediaCandidate) -> str:
    kind = _KIND_LABELS.get(candidate.kind, candidate.kind)
    quality = f" {candidate.height}p" if candidate.height else ""
    filename = _filename_for(candidate)
    return texts.PAGE_SCAN_BUTTON.format(index=index, kind=kind, quality=quality, filename=filename)


def build_markup(candidates: Sequence[MediaCandidate]) -> list[list[Button]]:
    return [
        [Button.inline(button_label(index, candidate), encode("ps", str(index)))]
        for index, candidate in enumerate(candidates, start=1)
    ]


def parse_selection(data: bytes) -> int | None:
    try:
        parts = decode(data)
    except UnicodeDecodeError:
        return None
    if len(parts) != 2 or parts[0] != "ps":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None
