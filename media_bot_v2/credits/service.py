"""Credit/quota logic ported from the old bot's src/database/model.py.

Behavior is intentionally identical to the old bot (same deduction order,
same rounding, same bandwidth gate) since users' existing free/paid balances
must keep meaning the same thing after cutover:

- Deduction order is always free credits first, then paid credits.
- 1 credit per 200 MB of actual file size, rounded up, minimum 1 credit if
  any bytes were transferred (see use_quota_dynamic in the old model.py).
- Owners (config.owner_ids) are exempt from all quota/bandwidth checks.
- When ENABLE_VIP is false, quota checks are a no-op (unlimited downloads).
"""

from __future__ import annotations

import math

from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.credits.exceptions import (
    BandwidthExhaustedException,
    CreditsExhaustedException,
)
from media_bot_v2.db.models import User
from media_bot_v2.db.session import session_scope


class CreditsService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        enable_vip: bool,
        owner_ids: list[int],
        free_bandwidth: int,
    ) -> None:
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
                return
            if user.is_blocked:
                raise Exception("המשתמש שלך נחסם. פנה למנהל.")
            if (user.free or 0) + (user.paid or 0) <= 0:
                raise CreditsExhaustedException("הקרדיטים שלך נגמרו.")
            if (user.paid or 0) <= 0 and (user.bandwidth_used or 0) >= self._free_bandwidth:
                raise BandwidthExhaustedException(
                    "הגעת למגבלת 2GB יומית למשתמשים חינמיים.\nלרכישת חבילה ללא הגבלה שלח /buy"
                )

    def use_quota_dynamic(self, user_id: int, file_sizes: list[int] | int) -> int:
        """Deduct credits for a completed transfer, return remaining credits."""
        if not self._enable_vip:
            return math.inf  # type: ignore[return-value]

        if isinstance(file_sizes, int):
            file_sizes = [file_sizes] if file_sizes > 0 else []

        total_mb = sum(file_sizes) / (1024 * 1024)
        credits_to_deduct = max(1, math.ceil(total_mb / 200)) if file_sizes else 0

        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if user is None:
                return 0
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
            if user is not None:
                user.bandwidth_used = (user.bandwidth_used or 0) + size
                user.total_bandwidth = (user.total_bandwidth or 0) + size

    def get_total_credits(self, user_id: int) -> int:
        if not self._enable_vip:
            return math.inf  # type: ignore[return-value]
        with session_scope(self._sessions) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            return (user.free or 0) + (user.paid or 0) if user else 0
