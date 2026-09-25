"""Download cache: (media reference + quality + delivery format) -> an
already-sent Telegram message, so a repeat request resends instead of
re-downloading.

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
cache hit re-sends the media of those messages to the requesting chat
(`TelethonUploader.send_cached`) - a server-side operation, no re-upload and
no "Forwarded from" header. This means caching is a no-op (always a miss)
when no archive channel is configured; there is no other stable place to
resend from.

The entry also keeps what is needed to rebuild the *requesting user's*
caption on a hit (per-message kind/duration/resolution/part position, the
title and description), so a hit looks the same as a fresh delivery and never
reuses the archive's operator caption. Rows written before this metadata
existed (only a `title`) still decode; they get a basic caption.

Reading back an old bot's entry (or anything that doesn't match this
store's own JSON shape) is treated as a cache miss rather than an error -
after cutover this table holds both bots' writes, and a foreign/corrupted
row must fall back to a fresh download, not crash the request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.db.models import VideoCache
from media_bot_v2.db.session import session_scope


def compute_cache_key(
    media_ref: str, quality: str, send_as: str = "video", subtitles: bool = False
) -> str:
    """media_ref is a platform-stable id when the engine can extract one
    (e.g. a YouTube video id), falling back to the raw URL otherwise.

    `send_as` and `subtitles` are part of the key because they change what
    was delivered (and therefore archived): a user who switches from "video"
    to "file" must not be served the earlier video message. The defaults
    (video, no subtitles) hash exactly as before this parameter existed, so
    existing cache rows stay valid for them.
    """
    raw = f"{media_ref}:{quality}"
    if send_as != "video":
        raw += f":{send_as}"
    if subtitles:
        raw += ":subs"
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


@dataclass
class CachedItem:
    """One archived message: what it was, for rebuilding its caption."""

    kind: str = "video"
    duration: int = 0
    width: int = 0
    height: int = 0
    part_index: int = 0  # 1-based position inside a split file; 0 = not a part
    part_total: int = 0
    part_label: str | None = None  # caption prefix for parts (video or document style)


@dataclass
class CacheEntry:
    archive_chat: str
    message_ids: list[int]
    title: str | None
    items: list[CachedItem] = field(default_factory=list)
    description: str | None = None
    subtitle_ids: list[int] = field(default_factory=list)
    subtitle_names: list[str] = field(default_factory=list)


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

    def put(
        self,
        cache_key: str,
        *,
        archive_chat: str,
        message_ids: list[int],
        title: str | None,
        items: list[CachedItem] | None = None,
        description: str | None = None,
        subtitle_ids: list[int] | None = None,
        subtitle_names: list[str] | None = None,
    ) -> None:
        payload = json.dumps({"archive_chat": archive_chat, "message_ids": message_ids})
        meta = json.dumps(
            {
                "title": title,
                "items": [asdict(item) for item in items or []],
                "description": description,
                "subtitle_ids": subtitle_ids or [],
                "subtitle_names": subtitle_names or [],
            }
        )
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
    if not isinstance(meta, dict):
        meta = {}
    return CacheEntry(
        archive_chat=archive_chat,
        message_ids=message_ids,
        title=meta.get("title"),
        items=_decode_items(meta.get("items"), len(message_ids)),
        description=meta.get("description") if isinstance(meta.get("description"), str) else None,
        subtitle_ids=_int_list(meta.get("subtitle_ids")),
        subtitle_names=[n for n in (meta.get("subtitle_names") or []) if isinstance(n, str)],
    )


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int)]


def _decode_items(raw: object, expected: int) -> list[CachedItem]:
    """Items are optional (old rows have none); a malformed or mismatched
    list is dropped rather than trusted, giving those messages a basic caption."""
    if not isinstance(raw, list) or len(raw) != expected:
        return []
    items: list[CachedItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return []
        try:
            items.append(
                CachedItem(
                    kind=str(entry.get("kind") or "video"),
                    duration=int(entry.get("duration") or 0),
                    width=int(entry.get("width") or 0),
                    height=int(entry.get("height") or 0),
                    part_index=int(entry.get("part_index") or 0),
                    part_total=int(entry.get("part_total") or 0),
                    part_label=entry.get("part_label") if isinstance(entry.get("part_label"), str) else None,
                )
            )
        except (TypeError, ValueError):
            return []
    return items
