"""Tests for provider health tracking and cooldown management in database."""

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from media_bot_v2.db.models import Base, ProviderHealth
from media_bot_v2.providers.base import BaseProvider, ProviderResult
from media_bot_v2.providers.health import ProviderHealthTracker


class _DummyProvider(BaseProvider):
    def __init__(self, name: str, supported_platforms=("tiktok",)):
        super().__init__()
        self.name = name
        self.supported_platforms = supported_platforms

    def matches(self, url: str) -> bool:
        return True

    async def fetch(self, url: str) -> ProviderResult:
        return ProviderResult(provider=self.name, media_urls=["http://example.com/test.mp4"])


def _make_tracker(threshold=3, cooldown=300):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    tracker = ProviderHealthTracker(
        session_factory,
        failure_threshold=threshold,
        cooldown_seconds=cooldown,
    )
    return tracker, session_factory


def test_record_success_updates_metrics_and_resets_failures():
    tracker, session_factory = _make_tracker()
    now = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)

    # First record success
    tracker.record_success("tikwm", "tiktok", response_time=1.25, now=now)

    with session_factory() as session:
        record = session.scalar(
            select(ProviderHealth).where(
                ProviderHealth.provider == "tikwm",
                ProviderHealth.platform == "tiktok",
            )
        )
        assert record is not None
        assert record.total_attempts == 1
        assert record.successes == 1
        assert record.total_response_time == 1.25
        assert record.consecutive_failures == 0
        assert record.disabled_until is None

    # Second record success adds to stats
    tracker.record_success("tikwm", "tiktok", response_time=0.75, now=now)
    with session_factory() as session:
        record = session.scalar(
            select(ProviderHealth).where(
                ProviderHealth.provider == "tikwm",
                ProviderHealth.platform == "tiktok",
            )
        )
        assert record.total_attempts == 2
        assert record.successes == 2
        assert record.total_response_time == 2.0


def test_record_failure_increments_consecutive_and_suppresses_at_threshold():
    tracker, session_factory = _make_tracker(threshold=3, cooldown=300)
    now = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)

    # 1st failure
    tracker.record_failure("tikwm", "tiktok", "Error 500", now=now)
    assert not tracker.is_suppressed("tikwm", "tiktok", now=now)

    # 2nd failure
    tracker.record_failure("tikwm", "tiktok", "Error 502", now=now)
    assert not tracker.is_suppressed("tikwm", "tiktok", now=now)

    with session_factory() as session:
        record = session.scalar(select(ProviderHealth))
        assert record.total_attempts == 2
        assert record.consecutive_failures == 2
        assert record.disabled_until is None

    # 3rd failure reaches threshold of 3 -> triggers suppression
    tracker.record_failure("tikwm", "tiktok", "Error 503", now=now)
    assert tracker.is_suppressed("tikwm", "tiktok", now=now)

    with session_factory() as session:
        record = session.scalar(select(ProviderHealth))
        assert record.total_attempts == 3
        disabled_until = record.disabled_until
        if disabled_until and disabled_until.tzinfo is None:
            disabled_until = disabled_until.replace(tzinfo=dt.UTC)
        assert disabled_until == now + dt.timedelta(seconds=300)
        assert record.last_error == "Error 503"


def test_cooldown_expiry_unsuppresses_provider():
    tracker, _ = _make_tracker(threshold=2, cooldown=300)
    t0 = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)

    tracker.record_failure("tikwm", "tiktok", "Fail 1", now=t0)
    tracker.record_failure("tikwm", "tiktok", "Fail 2", now=t0)

    # At t0 + 100s, still cooling down
    assert tracker.is_suppressed("tikwm", "tiktok", now=t0 + dt.timedelta(seconds=100))

    # At t0 + 301s, cooldown has elapsed
    assert not tracker.is_suppressed("tikwm", "tiktok", now=t0 + dt.timedelta(seconds=301))


def test_order_for_filters_suppressed_providers():
    tracker, _ = _make_tracker(threshold=2, cooldown=300)
    t0 = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)

    p1 = _DummyProvider("tikwm")
    p2 = _DummyProvider("tikdownloader")
    p3 = _DummyProvider("musicaldown")

    candidates = [p1, p2, p3]

    # Suppress p2
    tracker.record_failure("tikdownloader", "tiktok", "Err", now=t0)
    tracker.record_failure("tikdownloader", "tiktok", "Err", now=t0)

    # During cooldown, p2 is omitted from rotation
    ordered = tracker.order_for("tiktok", candidates, now=t0 + dt.timedelta(seconds=50))
    ordered_names = [p.name for p in ordered]
    assert "tikdownloader" not in ordered_names
    assert ordered_names == ["tikwm", "musicaldown"]

    # After cooldown expires, p2 returns to rotation
    ordered_later = tracker.order_for("tiktok", candidates, now=t0 + dt.timedelta(seconds=350))
    ordered_later_names = [p.name for p in ordered_later]
    assert "tikdownloader" in ordered_later_names


def test_order_for_sorts_by_success_rate_and_response_time():
    tracker, _ = _make_tracker()
    now = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)

    fast = _DummyProvider("fast_provider")
    slow = _DummyProvider("slow_provider")
    flaky = _DummyProvider("flaky_provider")

    # fast: 10/10 success, 0.5s avg
    for _ in range(10):
        tracker.record_success("fast_provider", "tiktok", 0.5, now=now)

    # slow: 10/10 success, 2.5s avg
    for _ in range(10):
        tracker.record_success("slow_provider", "tiktok", 2.5, now=now)

    # flaky: 5/10 success (alternating so consecutive failures <= 1, not suppressed)
    for _ in range(5):
        tracker.record_success("flaky_provider", "tiktok", 0.2, now=now)
        tracker.record_failure("flaky_provider", "tiktok", "err", now=now)

    candidates = [flaky, slow, fast]
    ordered = tracker.order_for("tiktok", candidates, now=now)

    # fast (100%, 0.5s) > slow (100%, 2.5s) > flaky (50%)
    assert [p.name for p in ordered] == ["fast_provider", "slow_provider", "flaky_provider"]
