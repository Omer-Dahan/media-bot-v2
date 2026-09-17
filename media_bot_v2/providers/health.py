"""Provider health tracking and adaptive ordering in the database."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from media_bot_v2.db.models import ProviderHealth
from media_bot_v2.db.session import session_scope
from media_bot_v2.providers.base import BaseProvider

logger = logging.getLogger(__name__)


@dataclass
class _HealthSnapshot:
    provider: str
    total_attempts: int
    successes: int
    total_response_time: float
    disabled_until: dt.datetime | None


class ProviderHealthTracker:
    """Tracks provider performance and manages suppression/cooldown in the DB."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        failure_threshold: int = 3,
        cooldown_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds

    def record_success(
        self,
        provider: str,
        platform: str,
        response_time: float,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        """Record a successful fetch, resetting failures and cooldown."""
        current_time = now or dt.datetime.now(dt.UTC)
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(ProviderHealth).where(
                    ProviderHealth.provider == provider,
                    ProviderHealth.platform == platform,
                )
            )
            elapsed = max(0.0, float(response_time))
            if record is None:
                record = ProviderHealth(
                    provider=provider,
                    platform=platform,
                    total_attempts=1,
                    successes=1,
                    total_response_time=elapsed,
                    consecutive_failures=0,
                    disabled_until=None,
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(record)
            else:
                record.total_attempts += 1
                record.successes += 1
                record.total_response_time += elapsed
                record.consecutive_failures = 0
                record.disabled_until = None
                record.updated_at = current_time

    def record_failure(
        self,
        provider: str,
        platform: str,
        error_message: str | None = None,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        """Record a failure, incrementing consecutive failures and suppressing if threshold reached."""
        current_time = now or dt.datetime.now(dt.UTC)
        err_text = str(error_message)[:500] if error_message else None

        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(ProviderHealth).where(
                    ProviderHealth.provider == provider,
                    ProviderHealth.platform == platform,
                )
            )
            if record is None:
                record = ProviderHealth(
                    provider=provider,
                    platform=platform,
                    total_attempts=1,
                    successes=0,
                    total_response_time=0.0,
                    consecutive_failures=1,
                    last_error=err_text,
                    disabled_until=None,
                    created_at=current_time,
                    updated_at=current_time,
                )
                session.add(record)
            else:
                record.total_attempts += 1
                record.consecutive_failures += 1
                record.last_error = err_text
                record.updated_at = current_time

            if record.consecutive_failures >= self._failure_threshold:
                record.disabled_until = current_time + dt.timedelta(
                    seconds=self._cooldown_seconds
                )
                logger.warning(
                    "Provider %s on %s reached %d consecutive failures; suppressed until %s",
                    provider,
                    platform,
                    record.consecutive_failures,
                    record.disabled_until,
                )

    def is_suppressed(
        self,
        provider: str,
        platform: str,
        *,
        now: dt.datetime | None = None,
    ) -> bool:
        """Return True if provider is currently suppressed due to consecutive failures."""
        current_time = now or dt.datetime.now(dt.UTC)
        with session_scope(self._session_factory) as session:
            record = session.scalar(
                select(ProviderHealth).where(
                    ProviderHealth.provider == provider,
                    ProviderHealth.platform == platform,
                )
            )
            if not record or not record.disabled_until:
                return False
            disabled_until = record.disabled_until
            if disabled_until.tzinfo is None:
                disabled_until = disabled_until.replace(tzinfo=dt.UTC)
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=dt.UTC)
            return disabled_until > current_time

    def order_for(
        self,
        platform: str,
        candidates: Sequence[BaseProvider],
        *,
        now: dt.datetime | None = None,
    ) -> list[BaseProvider]:
        """Order providers by success rate and response time, suppressing failed ones in cooldown."""
        if not candidates:
            return []

        current_time = now or dt.datetime.now(dt.UTC)
        candidate_names = {c.name for c in candidates}

        with session_scope(self._session_factory) as session:
            records = session.scalars(
                select(ProviderHealth).where(
                    ProviderHealth.platform == platform,
                    ProviderHealth.provider.in_(candidate_names),
                )
            ).all()
            health_by_name = {
                r.provider: _HealthSnapshot(
                    provider=r.provider,
                    total_attempts=r.total_attempts,
                    successes=r.successes,
                    total_response_time=r.total_response_time,
                    disabled_until=r.disabled_until,
                )
                for r in records
            }

        # Filter out suppressed providers
        active_candidates: list[BaseProvider] = []
        for candidate in candidates:
            record = health_by_name.get(candidate.name)
            if record and record.disabled_until:
                disabled_until = record.disabled_until
                if disabled_until.tzinfo is None:
                    disabled_until = disabled_until.replace(tzinfo=dt.UTC)
                if current_time.tzinfo is None:
                    current_time = current_time.replace(tzinfo=dt.UTC)
                if disabled_until > current_time:
                    # Still in cooldown, suppress
                    continue
            active_candidates.append(candidate)

        # Sort key:
        # 1. Success rate descending (default 1.0 for untried)
        # 2. Average response time ascending (default 0.0 for untried)
        # 3. Initial configured order index ascending
        candidate_index = {c.name: i for i, c in enumerate(candidates)}

        def sort_key(provider: BaseProvider) -> tuple[float, float, int]:
            record = health_by_name.get(provider.name)
            idx = candidate_index.get(provider.name, 0)
            if record is None or record.total_attempts == 0:
                return (-1.0, 0.0, idx)
            success_rate = record.successes / record.total_attempts
            avg_time = (
                record.total_response_time / record.successes
                if record.successes > 0
                else 999.0
            )
            return (-success_rate, avg_time, idx)

        active_candidates.sort(key=sort_key)
        return active_candidates
