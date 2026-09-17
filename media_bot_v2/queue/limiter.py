"""Per-user and global concurrency limiting for downloads.

Two `asyncio.Semaphore` layers: a per-user cap (stops one user from hogging
every slot) and a global cap (bounds total concurrent downloads regardless
of how many distinct users are active), mirroring the old bot's
`engine/concurrency.py` two-tier design without its free/paid-tier split
(that tier logic lives in `credits/service.py` here, not the limiter).

The per-user semaphore is acquired before the global one and released after
it, so a user waiting on their own cap never holds a global slot, but a
download that already holds the global slot always releases its per-user
slot too - `slot()` guarantees both are released on every exit path
(success, exception, or `asyncio.CancelledError` from a cancelled task),
never just the happy path.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class ConcurrencyLimiter:
    def __init__(self, *, global_limit: int, per_user_limit: int) -> None:
        self._global = asyncio.Semaphore(global_limit)
        self._per_user_limit = per_user_limit
        self._per_user: dict[int, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(per_user_limit)
        )

    def for_user(self, user_id: int) -> asyncio.Semaphore:
        return self._per_user[user_id]

    @asynccontextmanager
    async def slot(
        self,
        user_id: int,
        *,
        on_wait: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        """Acquire one global slot and one per-user slot, releasing both on
        exit. Calls `on_wait` (once, before blocking) if either cap is
        already exhausted, so the caller can tell the user they're queued
        instead of leaving them staring at a message that hasn't moved.
        """
        user_semaphore = self.for_user(user_id)
        must_wait = self._global.locked() or user_semaphore.locked()
        if must_wait and on_wait is not None:
            await on_wait()

        await user_semaphore.acquire()
        try:
            await self._global.acquire()
            try:
                yield
            finally:
                self._global.release()
        finally:
            user_semaphore.release()
