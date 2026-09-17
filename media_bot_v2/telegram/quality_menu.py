"""YouTube quality-select menu: buttons + the URL cache behind them.

UI ported from the old bot's quality-select keyboard (src/main.py:668-693).
The old bot's `_youtube_url_cache` (main.py:88) was an unbounded,
non-expiring, in-process dict keyed by url_hash -> url; entries leaked
forever if a user never clicked a button. QualitySelectionStore replaces it
with a TTL-bounded, size-capped store (spec/INVENTORY.md section 3).

The YouTube engine itself lands in M2 - this module only builds the menu;
nothing here downloads anything.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict

from telethon import Button

from media_bot_v2.telegram.callback_data import encode


class QualitySelectionStore:
    def __init__(self, *, ttl_seconds: int = 3600, max_entries: int = 2000) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def put(self, url: str) -> str:
        self._evict_expired()
        url_hash = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]
        self._entries[url_hash] = (url, time.monotonic())
        self._entries.move_to_end(url_hash)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return url_hash

    def get(self, url_hash: str) -> str | None:
        self._evict_expired()
        entry = self._entries.get(url_hash)
        return entry[0] if entry else None

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_, ts) in self._entries.items() if now - ts > self._ttl]
        for key in expired:
            del self._entries[key]


def build_quality_markup(url_hash: str) -> list[list[Button]]:
    return [
        [
            Button.inline("🎬 1080p HD", encode("ytq", "1080", url_hash)),
            Button.inline("🎬 720p", encode("ytq", "720", url_hash)),
        ],
        [
            Button.inline("🎬 480p", encode("ytq", "480", url_hash)),
            Button.inline("🎬 360p", encode("ytq", "360", url_hash)),
        ],
        [Button.inline("🎵 שמע בלבד", encode("ytq", "audio", url_hash))],
    ]
