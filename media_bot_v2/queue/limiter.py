"""Per-user and global concurrency limiting for downloads.

Placeholder for M1: real implementation gates concurrent downloads using
asyncio.Semaphore instances, mirroring the old bot's engine/concurrency.py
(per-user cap + global worker cap from the WORKERS env var).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class ConcurrencyLimiter:
    def __init__(self, *, global_limit: int, per_user_limit: int) -> None:
        self._global = asyncio.Semaphore(global_limit)
        self._per_user_limit = per_user_limit
        self._per_user: dict[int, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(per_user_limit)
        )

    def for_user(self, user_id: int) -> asyncio.Semaphore:
        return self._per_user[user_id]
