"""Download cache: (media reference + quality) -> an already-sent Telegram
message, so a repeat request resends instead of re-downloading.

Reuses the old bot's `video_cache` table (`db/models.py:VideoCache`) since
the schema is locked to match production. The *content* stored in it is not
schema-compatible with the old bot's entries, though: the old bot ran on
Kurigram/Pyrofork, which (like python-telegram-bot) synthesizes a portable
Bot-API-style `file_id` string. Telethon has no equivalent - MTProto exposes
raw `Document`/`Photo` references (id/access_hash/file_reference) that
aren't safely picklable into a stable string across sessions.

Instead, this store leans on the archive-channel forward the pipeline
already does for every successful upload (`telegram/uploader.py`): the
cache entry records which message(s) landed in the archive channel, and a
cache hit re-forwards those same messages to the requesting chat - a
server-side operation, no re-upload, same mechanism `forward_to_archive`
already relies on. This means caching is a no-op (always a miss) when no
archive channel is configured; there is no other stable place to forward
from.

Reading back an old bot's entry (or anything that doesn't match this
store's own JSON shape) is treated as a cache miss rather than an error -
after cutover this table holds both bots' writes, and a foreign/corrupted
row must fall back to a fresh download, not crash the request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.db.models import VideoCache
from media_bot_v2.db.session import session_scope


def compute_cache_key(media_ref: str, quality: str) -> str:
    """media_ref is a platform-stable id when the engine can extract one
    (e.g. a YouTube video id), falling back to the raw URL otherwise."""
    digest = hashlib.md5(f"{media_ref}:{quality}".encode(), usedforsecurity=False)
    return digest.hexdigest()


@dataclass
class CacheEntry:
    archive_chat: str
    message_ids: list[int]
    title: str | None


class VideoCacheStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def get(self, cache_key: str) -> CacheEntry | None:
        with session_scope(self._sessions) as session:
            row = session.query(VideoCache).filter(VideoCache.cache_key == cache_key).first()
            if row is None:
                return None
            entry = _decode_entry(row.file_id, row.meta)
            if entry is None:
                return None
            return entry

    def delete(self, cache_key: str) -> None:
        with session_scope(self._sessions) as session:
            session.query(VideoCache).filter(VideoCache.cache_key == cache_key).delete()

    def put(self, cache_key: str, *, archive_chat: str, message_ids: list[int], title: str | None) -> None:
        payload = json.dumps({"archive_chat": archive_chat, "message_ids": message_ids})
        meta = json.dumps({"title": title})
        with session_scope(self._sessions) as session:
            row = session.query(VideoCache).filter(VideoCache.cache_key == cache_key).first()
            if row is not None:
                row.file_id = payload
                row.meta = meta
            else:
                session.add(VideoCache(cache_key=cache_key, file_id=payload, meta=meta))


def _decode_entry(file_id_json: str, meta_json: str) -> CacheEntry | None:
    try:
        payload = json.loads(file_id_json)
        meta = json.loads(meta_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    archive_chat = payload.get("archive_chat")
    message_ids = payload.get("message_ids")
    if not isinstance(archive_chat, str) or not isinstance(message_ids, list):
        return None
    if not all(isinstance(mid, int) for mid in message_ids):
        return None
    title = meta.get("title") if isinstance(meta, dict) else None
    return CacheEntry(archive_chat=archive_chat, message_ids=message_ids, title=title)
