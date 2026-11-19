"""Credit deductions, weekly rollover, and quota enforcement."""

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.credits.service import CreditsService, WeeklyStatus
from media_bot_v2.db.models import Base, User

GIB = 1024**3
WEEKLY_BYTES = 10 * GIB
JERUSALEM = ZoneInfo("Asia/Jerusalem")
SUNDAY = datetime(2026, 10, 25, tzinfo=JERUSALEM)
WEEK = "2026-10-25"
PREVIOUS_WEEK = "2026-10-18"


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def clock():
    return [SUNDAY]


@pytest.fixture
def service(session_factory, clock):
    return CreditsService(
        session_factory,
        enable_vip=True,
        owner_ids=[999],
        now=lambda: clock[0],
    )


def _add_user(session_factory, **kwargs):
    defaults = {"user_id": 1, "free": 3, "paid": 0, "bandwidth_used": 0, "total_bandwidth": 0, "is_blocked": 0}
    defaults["config"] = {"credits_week": WEEK}
    defaults.update(kwargs)
    with session_factory() as session:
        user = User(**defaults)
        session.add(user)
        session.commit()


def test_check_quota_passes_with_credits(session_factory, service):
    _add_user(session_factory)
    service.check_quota(1)  # should not raise


def test_check_quota_raises_when_exhausted(session_factory, service):
    _add_user(session_factory, free=0, paid=0, bandwidth_used=5 * GIB)
    with pytest.raises(CreditsExhaustedException) as exc:
        service.check_quota(1)
    assert exc.value.used_bytes == 5 * GIB
    assert exc.value.quota_bytes == WEEKLY_BYTES
    assert exc.value.next_reset_at == datetime(2026, 11, 1, tzinfo=JERUSALEM)


def test_check_quota_raises_on_bandwidth_cap_for_free_users(session_factory, service):
    _add_user(session_factory, free=3, paid=0, bandwidth_used=WEEKLY_BYTES)
    with pytest.raises(BandwidthExhaustedException):
        service.check_quota(1)


def test_check_quota_ignores_bandwidth_cap_for_paid_users(session_factory, service):
    _add_user(session_factory, free=0, paid=5, bandwidth_used=WEEKLY_BYTES)
    service.check_quota(1)  # should not raise


def test_check_quota_raises_user_blocked_for_blocked_user(session_factory, service):
    _add_user(session_factory, is_blocked=1)
    with pytest.raises(UserBlockedException):
        service.check_quota(1)


def test_owner_bypasses_all_checks(session_factory, service):
    _add_user(session_factory, user_id=999, free=0, paid=0)
    service.check_quota(999)  # should not raise


def test_use_quota_dynamic_deducts_free_before_paid(session_factory, service):
    _add_user(session_factory, free=1, paid=5)
    # 250MB -> ceil(250/200) = 2 credits: 1 from free, 1 from paid
    remaining = service.use_quota_dynamic(1, [250 * 1024 * 1024])
    assert remaining == 4  # 0 free + 4 paid


def test_use_quota_dynamic_minimum_one_credit_for_small_file(session_factory, service):
    _add_user(session_factory, free=3, paid=0)
    remaining = service.use_quota_dynamic(1, [1024])  # 1KB, still costs 1 credit
    assert remaining == 2


def test_use_quota_dynamic_zero_bytes_deducts_nothing(session_factory, service):
    _add_user(session_factory, free=3, paid=0)
    remaining = service.use_quota_dynamic(1, [])
    assert remaining == 3


def test_check_quota_raises_loudly_for_unknown_user_instead_of_skipping(session_factory, service):
    """Covers finding 1: a user who never ran get_or_create_user (e.g. sent
    a direct link before /start) must not be treated as unlimited/exempt -
    that silent skip is exactly what let new users download for free."""
    with pytest.raises(RuntimeError):
        service.check_quota(999999)  # no such user_id in the DB


def test_use_quota_dynamic_raises_loudly_for_unknown_user(session_factory, service):
    with pytest.raises(RuntimeError):
        service.use_quota_dynamic(999999, [1024])


def test_add_bandwidth_used_raises_loudly_for_unknown_user(session_factory, service):
    with pytest.raises(RuntimeError):
        service.add_bandwidth_used(999999, 1024)


def test_disabled_vip_skips_all_enforcement():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = CreditsService(factory, enable_vip=False, owner_ids=[], free_weekly_bytes=100)
    service.check_quota(42)  # no user row exists at all; must not raise


def test_get_total_credits_returns_inf_when_vip_disabled(session_factory):
    service = CreditsService(session_factory, enable_vip=False, owner_ids=[], free_weekly_bytes=100)
    assert service.get_total_credits(42) == math.inf


def test_get_total_credits_returns_inf_for_owner_even_with_zero_credits(session_factory, service):
    _add_user(session_factory, user_id=999, free=0, paid=0)
    assert service.get_total_credits(999) == math.inf


def test_get_total_credits_returns_balance_for_normal_user(session_factory, service):
    _add_user(session_factory, user_id=1, free=3, paid=2)
    assert service.get_total_credits(1) == 5


def test_get_total_credits_returns_zero_when_user_has_no_credits(session_factory, service):
    _add_user(session_factory, user_id=1, free=0, paid=0)
    assert service.get_total_credits(1) == 0


def test_get_total_credits_returns_zero_for_unknown_user_when_vip_enabled(session_factory, service):
    assert service.get_total_credits(999999) == 0



def _balance(session_factory):
    with session_factory() as session:
        user = session.query(User).one()
        return user.free, user.paid, user.bandwidth_used, user.total_bandwidth, user.config


def test_same_week_preserves_balance(session_factory, service):
    _add_user(session_factory, free=1)
    assert service.get_total_credits(1) == 1
    assert _balance(session_factory) == (1, 0, 0, 0, {"credits_week": WEEK})


def test_rollover_at_sunday_midnight(session_factory, service, clock):
    _add_user(
        session_factory,
        free=0,
        bandwidth_used=5 * GIB,
        total_bandwidth=20 * GIB,
        config={"credits_week": PREVIOUS_WEEK, "keep": True},
    )
    clock[0] = SUNDAY.astimezone(UTC) - timedelta(seconds=1)
    assert service.get_total_credits(1) == 0
    assert _balance(session_factory) == (
        0, 0, 5 * GIB, 20 * GIB, {"credits_week": PREVIOUS_WEEK, "keep": True}
    )

    clock[0] += timedelta(seconds=1)
    service.check_quota(1)
    assert _balance(session_factory) == (52, 0, 0, 20 * GIB, {"credits_week": WEEK, "keep": True})


def test_paid_user_is_exempt_from_rollover(session_factory, service):
    _add_user(session_factory, free=1, paid=3, bandwidth_used=5 * GIB, config={"credits_week": PREVIOUS_WEEK})
    service.check_quota(1)
    assert _balance(session_factory) == (1, 3, 5 * GIB, 0, {"credits_week": PREVIOUS_WEEK})


@pytest.mark.parametrize("config", [{}, None])
def test_rollover_initializes_existing_user_once(session_factory, service, config):
    _add_user(session_factory, free=0, config=config)
    assert service.get_total_credits(1) == 52
    assert _balance(session_factory) == (52, 0, 0, 0, {"credits_week": WEEK})
    assert service.get_total_credits(1) == 52
    assert _balance(session_factory) == (52, 0, 0, 0, {"credits_week": WEEK})


def test_rollover_before_deduction_keeps_charge_on_later_access(session_factory, service):
    _add_user(session_factory, free=0, config={"credits_week": PREVIOUS_WEEK})
    assert service.use_quota_dynamic(1, [1024]) == 51
    assert service.get_total_credits(1) == 51


def test_rollover_before_recording_bandwidth(session_factory, service):
    _add_user(
        session_factory, free=0, bandwidth_used=5 * GIB, total_bandwidth=5 * GIB,
        config={"credits_week": PREVIOUS_WEEK},
    )
    service.add_bandwidth_used(1, 1024)
    assert _balance(session_factory) == (52, 0, 1024, 5 * GIB + 1024, {"credits_week": WEEK})


def test_weekly_status_for_paid_user_includes_paid_credits_and_clamps_usage(session_factory, service):
    _add_user(session_factory, free=1, paid=3, bandwidth_used=11 * GIB)
    assert service.weekly_status(1) == WeeklyStatus(
        credits_left=4,
        quota_credits=52,
        quota_bytes=WEEKLY_BYTES,
        used_bytes=WEEKLY_BYTES,
        next_reset_at=datetime(2026, 11, 1, tzinfo=JERUSALEM),
    )


def test_weekly_status_rolls_over(session_factory, service):
    _add_user(session_factory, free=0, bandwidth_used=5 * GIB, config={"credits_week": PREVIOUS_WEEK})
    assert service.weekly_status(1) == WeeklyStatus(
        credits_left=52,
        quota_credits=52,
        quota_bytes=WEEKLY_BYTES,
        used_bytes=0,
        next_reset_at=datetime(2026, 11, 1, tzinfo=JERUSALEM),
    )


def test_weekly_status_is_none_for_owner(service):
    assert service.weekly_status(999) is None


def test_weekly_status_is_none_when_vip_disabled(session_factory, clock):
    service = CreditsService(session_factory, enable_vip=False, owner_ids=[], now=lambda: clock[0])
    assert service.weekly_status(1) is None


def test_credits_exhausted_exception_accepts_message_only():
    exc = CreditsExhaustedException("No credits")
    assert str(exc) == "No credits"
    assert exc.used_bytes is exc.quota_bytes is exc.next_reset_at is None
