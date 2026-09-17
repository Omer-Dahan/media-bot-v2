"""QualitySelectionStore: TTL-bounded replacement for the old bot's unbounded
_youtube_url_cache (src/main.py:88, see spec/INVENTORY.md section 3)."""

from media_bot_v2.telegram.quality_menu import QualitySelectionStore, build_quality_markup


def test_put_then_get_round_trips_the_url():
    store = QualitySelectionStore()
    url_hash = store.put("https://youtube.com/watch?v=abc123")
    assert store.get(url_hash) == "https://youtube.com/watch?v=abc123"


def test_get_returns_none_for_unknown_hash():
    store = QualitySelectionStore()
    assert store.get("deadbeef") is None


def test_entries_expire_after_ttl(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("media_bot_v2.telegram.quality_menu.time.monotonic", lambda: clock[0])

    store = QualitySelectionStore(ttl_seconds=10)
    url_hash = store.put("https://youtube.com/watch?v=abc123")

    clock[0] = 100.0
    assert store.get(url_hash) is None


def test_store_evicts_oldest_when_over_capacity():
    store = QualitySelectionStore(max_entries=2)
    h1 = store.put("https://youtube.com/watch?v=one")
    store.put("https://youtube.com/watch?v=two")
    store.put("https://youtube.com/watch?v=three")
    assert store.get(h1) is None  # evicted, oldest


def test_build_quality_markup_has_five_quality_buttons_with_url_hash():
    rows = build_quality_markup("abcd1234")
    buttons = [b for row in rows for b in row]
    assert len(buttons) == 5
    for button in buttons:
        assert b"abcd1234" in button.type.data
