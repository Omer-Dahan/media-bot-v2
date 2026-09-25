"""Credit/quota logic ported from the old bot's src/database/model.py.

Behavior is intentionally identical to the old bot (same deduction order,
same rounding, same bandwidth gate) since users' existing free/paid balances
must keep meaning the same thing after cutover:

- Deduction order is always free credits first, then paid credits.
- Credits are a unit of *volume*, not of requests or parts: one credit per
  MB_PER_CREDIT (default 200) MB of everything delivered in a request,
  rounded up, minimum 1 credit for any request that delivered a file (see
  use_quota_dynamic in the old model.py). `credits_for_sizes` is the single
  place this is computed.
- Owners (config.owner_ids) are exempt from all quota/bandwidth checks.
- When ENABLE_VIP is false, quota checks are a no-op (unlimited downloads).
"""

from __future__ import annotations

import math

from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
    UserBlockedException,
)
from media_bot_v2.db.models import User
from media_bot_v2.db.session import session_scope

BYTES_PER_MB = 1024 * 1024
DEFAULT_MB_PER_CREDIT = 200


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
        free_bandwidth: int,
        mb_per_credit: int = DEFAULT_MB_PER_CREDIT,
    ) -> None:
        if mb_per_credit <= 0:
            raise ValueError(f"mb_per_credit must be positive, got {mb_per_credit}")
        self._mb_per_credit = mb_per_credit
        self._sessions = session_factory
        self._enable_vip = enable_vip
        self._owner_ids = set(owner_ids)
        self._free_bandwidth = free_bandwidth

    def check_quota(self, user_id: int) -> None:
        if not self._enable_vip or user_id in self._owner_ids:
            return

        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                raise RuntimeError(
                    f"check_quota called for unknown user_id={user_id}; the caller must "
                    "get_or_create_user before running any quota-gated action"
                )
            if user.is_blocked:
                raise UserBlockedException("המשתמש שלך נחסם. פנה למנהל.")
            if (user.free or 0) + (user.paid or 0) <= 0:
                raise CreditsExhaustedException("הקרדיטים שלך נגמרו.")
            if (user.paid or 0) <= 0 and (user.bandwidth_used or 0) >= self._free_bandwidth:
                raise BandwidthExhaustedException(
                    "הגעת למגבלת 2GB יומית למשתמשים חינמיים.\nלרכישת חבילה ללא הגבלה שלח /buy"
                )

    def use_quota_dynamic(self, user_id: int, file_sizes: list[int]) -> int:
        """Deduct credits for everything one request delivered, return remaining credits."""
        if not self._enable_vip:
            return math.inf  # type: ignore[return-value]

        credits_to_deduct = credits_for_sizes(file_sizes, self._mb_per_credit)

        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                raise RuntimeError(
                    f"use_quota_dynamic called for unknown user_id={user_id}; the caller must "
                    "get_or_create_user before running any quota-gated action"
                )
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
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                raise RuntimeError(
                    f"add_bandwidth_used called for unknown user_id={user_id}; the caller must "
                    "get_or_create_user before running any quota-gated action"
                )
            user.bandwidth_used = (user.bandwidth_used or 0) + size
            user.total_bandwidth = (user.total_bandwidth or 0) + size

    def get_total_credits(self, user_id: int) -> int | float:
        if not self._enable_vip or user_id in self._owner_ids:
            return math.inf
        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            return (user.free or 0) + (user.paid or 0) if user else 0

