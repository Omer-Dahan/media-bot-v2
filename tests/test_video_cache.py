"""VideoCacheStore: cache_key -> archive-channel message reference.

Covers the "treat anything that doesn't match our own JSON shape as a miss"
guarantee - after cutover this table holds rows the old bot wrote too
(a different, Pyrogram-style file_id format), and reading one of those must
fall back to a fresh download instead of raising."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.cache.video_cache import VideoCacheStore, compute_cache_key
from media_bot_v2.db.models import Base, VideoCache


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_compute_cache_key_is_stable_and_quality_sensitive():
    a = compute_cache_key("abc123", "720")
    b = compute_cache_key("abc123", "720")
    c = compute_cache_key("abc123", "1080")
    assert a == b
    assert a != c


def test_get_returns_none_for_missing_key(session_factory):
    store = VideoCacheStore(session_factory)
    assert store.get("nope") is None


def test_put_then_get_round_trips(session_factory):
    store = VideoCacheStore(session_factory)
    key = compute_cache_key("abc123", "720")
    store.put(key, archive_chat="@archive", message_ids=[10, 11], title="My Video")

    entry = store.get(key)
    assert entry is not None
    assert entry.archive_chat == "@archive"
    assert entry.message_ids == [10, 11]
    assert entry.title == "My Video"


def test_put_overwrites_an_existing_entry_for_the_same_key(session_factory):
    store = VideoCacheStore(session_factory)
    key = compute_cache_key("abc123", "720")
    store.put(key, archive_chat="@archive", message_ids=[1], title="Old")
    store.put(key, archive_chat="@archive", message_ids=[2, 3], title="New")

    entry = store.get(key)
    assert entry.message_ids == [2, 3]
    assert entry.title == "New"


def test_delete_removes_the_entry(session_factory):
    store = VideoCacheStore(session_factory)
    key = compute_cache_key("abc123", "720")
    store.put(key, archive_chat="@archive", message_ids=[1], title="X")

    store.delete(key)

    assert store.get(key) is None


def test_get_treats_a_foreign_format_row_as_a_miss_not_a_crash(session_factory):
    """Simulates a row written by the old bot's Pyrogram-style cache.py:
    file_id is a plain JSON list of opaque strings, not our {archive_chat,
    message_ids} shape."""
    with session_factory() as session:
        session.add(
            VideoCache(
                cache_key="foreign-key",
                file_id='["CAACAgIAAxkBAAI...some-pyrogram-file-id"]',
                meta='{"title": "Old bot cached this"}',
            )
        )
        session.commit()

    store = VideoCacheStore(session_factory)
    assert store.get("foreign-key") is None


def test_get_treats_malformed_json_as_a_miss_not_a_crash(session_factory):
    with session_factory() as session:
        session.add(VideoCache(cache_key="broken", file_id="not json at all", meta="{}"))
        session.commit()

    store = VideoCacheStore(session_factory)
    assert store.get("broken") is None
