"""Credit/quota behavior parity with the old bot's database/model.py logic."""

import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.credits.service import CreditsService
from media_bot_v2.db.models import Base, User


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def service(session_factory):
    return CreditsService(
        session_factory,
        enable_vip=True,
        owner_ids=[999],
        free_bandwidth=2_147_483_648,
    )


def _add_user(session_factory, **kwargs):
    defaults = {"user_id": 1, "free": 3, "paid": 0, "bandwidth_used": 0, "total_bandwidth": 0, "is_blocked": 0}
    defaults.update(kwargs)
    with session_factory() as session:
        user = User(**defaults)
        session.add(user)
        session.commit()


def test_check_quota_passes_with_credits(session_factory, service):
    _add_user(session_factory)
    service.check_quota(1)  # should not raise


def test_check_quota_raises_when_exhausted(session_factory, service):
    _add_user(session_factory, free=0, paid=0)
    with pytest.raises(CreditsExhaustedException):
        service.check_quota(1)


def test_check_quota_raises_on_bandwidth_cap_for_free_users(session_factory, service):
    _add_user(session_factory, free=3, paid=0, bandwidth_used=2_147_483_648)
    with pytest.raises(BandwidthExhaustedException):
        service.check_quota(1)


def test_check_quota_ignores_bandwidth_cap_for_paid_users(session_factory, service):
    _add_user(session_factory, free=0, paid=5, bandwidth_used=2_147_483_648)
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
    service = CreditsService(factory, enable_vip=False, owner_ids=[], free_bandwidth=100)
    service.check_quota(42)  # no user row exists at all; must not raise


def test_get_total_credits_returns_inf_when_vip_disabled(session_factory):
    service = CreditsService(session_factory, enable_vip=False, owner_ids=[], free_bandwidth=100)
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

