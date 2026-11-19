"""Volume-based credits and the weekly quota for free users."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.db.models import User
from media_bot_v2.db.session import session_scope
from media_bot_v2.telegram import texts

BYTES_PER_MB = 1024 * 1024
DEFAULT_MB_PER_CREDIT = 200

_WEEK_TZ = ZoneInfo("Asia/Jerusalem")


def week_start(now: datetime) -> date:
    local_date = now.astimezone(_WEEK_TZ).date()
    return local_date - timedelta(days=(local_date.weekday() + 1) % 7)


def next_reset_at(now: datetime) -> datetime:
    return datetime.combine(week_start(now) + timedelta(days=7), time.min, tzinfo=_WEEK_TZ)


@dataclass(frozen=True)
class WeeklyStatus:
    credits_left: int
    quota_credits: int
    quota_bytes: int
    used_bytes: int
    next_reset_at: datetime


def credits_for_sizes(file_sizes: list[int], mb_per_credit: int = DEFAULT_MB_PER_CREDIT) -> int:
    """Credits owed for one request that delivered files of `file_sizes` bytes.

    `max(1, ceil(total_MB / mb_per_credit))` over the summed size of every
    delivered file; 0 only when nothing was delivered. Sizes are summed
    before rounding, so splitting a file into parts never changes the price.
    """
    if mb_per_credit <= 0:
        raise ValueError(f"mb_per_credit must be positive, got {mb_per_credit}")
    if not file_sizes:
        return 0
    total_mb = sum(file_sizes) / BYTES_PER_MB
    return max(1, math.ceil(total_mb / mb_per_credit))


class CreditsService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        enable_vip: bool,
        owner_ids: list[int],
        free_weekly_bytes: int = 10 * 1024**3,
        mb_per_credit: int = DEFAULT_MB_PER_CREDIT,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if mb_per_credit <= 0:
            raise ValueError(f"mb_per_credit must be positive, got {mb_per_credit}")
        self._mb_per_credit = mb_per_credit
        self._sessions = session_factory
        self._enable_vip = enable_vip
        self._owner_ids = set(owner_ids)
        self._free_weekly_bytes = free_weekly_bytes
        self._weekly_credits = math.ceil(free_weekly_bytes / (mb_per_credit * BYTES_PER_MB))
        self._now = now if now is not None else lambda: datetime.now(_WEEK_TZ)

    def _rollover(self, _session: Session, user: User) -> datetime:
        now = self._now()
        if (user.paid or 0) > 0:
            return now
        week = week_start(now).isoformat()
        if (user.config or {}).get("credits_week") == week:
            return now
        user.free = self._weekly_credits
        user.bandwidth_used = 0
        user.config = {**(user.config or {}), "credits_week": week}
        return now

    def check_quota(self, user_id: int) -> None:
        if not self._enable_vip or user_id in self._owner_ids:
            return

        error = None
        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).with_for_update().first()
            if user is None:
                raise RuntimeError(
                    f"check_quota called for unknown user_id={user_id}; the caller must "
                    "get_or_create_user before running any quota-gated action"
                )
            now = self._rollover(session, user)
            if user.is_blocked:
                error = UserBlockedException("המשתמש שלך נחסם. פנה למנהל.")
            elif (user.free or 0) + (user.paid or 0) <= 0:
                error = CreditsExhaustedException(
                    "הקרדיטים שלך נגמרו.",
                    used_bytes=user.bandwidth_used or 0,
                    quota_bytes=self._free_weekly_bytes,
                    next_reset_at=next_reset_at(now),
                )
            elif (user.paid or 0) <= 0 and (user.bandwidth_used or 0) >= self._free_weekly_bytes:
                error = BandwidthExhaustedException(texts.BANDWIDTH_EXHAUSTED)

        # Commit any rollover before reporting a quota error.
        if error is not None:
            raise error

    def use_quota_dynamic(self, user_id: int, file_sizes: list[int]) -> int:
        """Deduct credits for everything one request delivered, return remaining credits."""
        if not self._enable_vip:
            return math.inf  # type: ignore[return-value]

        credits_to_deduct = credits_for_sizes(file_sizes, self._mb_per_credit)

        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).with_for_update().first()
            if user is None:
                raise RuntimeError(
                    f"use_quota_dynamic called for unknown user_id={user_id}; the caller must "
                    "get_or_create_user before running any quota-gated action"
                )
            self._rollover(session, user)
            for _ in range(credits_to_deduct):
                if (user.free or 0) > 0:
                    user.free -= 1
                elif (user.paid or 0) > 0:
                    user.paid -= 1
                else:
                    break
            return (user.free or 0) + (user.paid or 0)

    def add_bandwidth_used(self, user_id: int, size: int) -> None:
        if not self._enable_vip or size <= 0:
            return
        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).with_for_update().first()
            if user is None:
                raise RuntimeError(
                    f"add_bandwidth_used called for unknown user_id={user_id}; the caller must "
                    "get_or_create_user before running any quota-gated action"
                )
            self._rollover(session, user)
            user.bandwidth_used = (user.bandwidth_used or 0) + size
            user.total_bandwidth = (user.total_bandwidth or 0) + size

    def get_total_credits(self, user_id: int) -> int | float:
        if not self._enable_vip or user_id in self._owner_ids:
            return math.inf
        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).with_for_update().first()
            if user is None:
                return 0
            self._rollover(session, user)
            return (user.free or 0) + (user.paid or 0)

    def weekly_status(self, user_id: int) -> WeeklyStatus | None:
        if not self._enable_vip or user_id in self._owner_ids:
            return None
        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).with_for_update().first()
            if user is None:
                return None
            now = self._rollover(session, user)
            return WeeklyStatus(
                credits_left=(user.free or 0) + (user.paid or 0),
                quota_credits=self._weekly_credits,
                quota_bytes=self._free_weekly_bytes,
                used_bytes=min(user.bandwidth_used or 0, self._free_weekly_bytes),
                next_reset_at=next_reset_at(now),
            )

