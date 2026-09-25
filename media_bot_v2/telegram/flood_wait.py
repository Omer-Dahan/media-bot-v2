"""Central flood wait handling and Telegram API retry wrapper.

Telethon 1.45 by default sleeps silently up to 60s inside `TelegramClient._call`.
In this bot (matching the owner's production bot `Kraken`), `flood_sleep_threshold`
is set to 0 so flood wait errors surface immediately:
1. `FloodWaitError` and `FloodPremiumWaitError` (which inherits from `FloodError`,
   NOT `FloodWaitError` - Telethon rpcerrorlist.py:1639) are caught and handled.
2. In parallel upload: lane-shrinking (5 -> 2 -> 1) and single-part retries.
3. In general API calls: `call_with_flood_retry` waits for the required seconds
   and retries cleanly instead of crashing or leaving requests unhandled.
4. If a flood wait exceeds reasonable thresholds or retry limits, clean Hebrew
   feedback is returned to the user instead of cryptic failure messages.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon.errors import FloodPremiumWaitError, FloodWaitError

logger = logging.getLogger(__name__)

# Both errors have `exc.seconds` (int) and `exc.request`.
FLOOD_WAIT_ERRORS: tuple[type[Exception], ...] = (FloodWaitError, FloodPremiumWaitError)

# Maximum seconds we are willing to sleep for a single flood wait.
# Above this, we do not hang indefinitely; we abort and report to user.
MAX_FLOOD_WAIT_SECONDS: float = 120.0

# Thresholds for file size connection scaling (Kraken parity)
BIG_FILE_THRESHOLD: int = 10 * 1024 * 1024  # 10MB
MEDIUM_FILE_THRESHOLD: int = 100 * 1024 * 1024  # 100MB


def get_flood_wait_seconds(exc: BaseException) -> int:
    """Extract required wait seconds from FloodWaitError or FloodPremiumWaitError."""
    return int(getattr(exc, "seconds", 0) or 1)


def calculate_connections(file_size: int, max_connections: int = 5) -> int:
    """Determine the optimal number of TCP connections for a given file size.

    Adopts lessons from Kraken:
    - Small files (< 10MB): 1 connection (no extra TCP senders needed).
    - Medium files (< 100MB): at most 2 connections.
    - Large files (>= 100MB): up to max_connections (bounded by 1..5).
    Always clamped by `max_connections` (so restricted configs are respected).
    """
    if max_connections <= 1 or file_size < BIG_FILE_THRESHOLD:
        return 1
    if file_size < MEDIUM_FILE_THRESHOLD:
        return min(max_connections, 2)
    return max(1, min(5, max_connections))


async def call_with_flood_retry[T](
    func: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 5,
    max_wait_seconds: float = MAX_FLOOD_WAIT_SECONDS,
    max_total_wait_seconds: float | None = None,
    on_flood: Callable[..., Awaitable[None] | None] | None = None,
    on_flood_cleared: Callable[[], Awaitable[None] | None] | None = None,
    sleep_func: Callable[[float], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> T:
    """Execute an async Telegram API call, waiting and retrying if a flood wait occurs.

    Catches both `FloodWaitError` and `FloodPremiumWaitError`.

    Limits:
    - `max_retries`: number of flood waits tolerated before re-raising.
    - `max_wait_seconds`: ceiling for a SINGLE flood wait.
    - `max_total_wait_seconds`: optional ceiling for the CUMULATIVE sleep across
      retries. Default `None` means no artificial cumulative ceiling, so media
      delivery (`send_file`) waits out repeated floods instead of losing the
      whole download+upload. Interactive callers (quality menu / button clicks)
      opt in explicitly to keep the user from being blocked for long.
    """
    attempts = 0
    sleeper = sleep_func or asyncio.sleep
    total_slept: float = 0.0
    while True:
        try:
            return await func(*args, **kwargs)
        except FLOOD_WAIT_ERRORS as exc:
            attempts += 1
            wait_seconds = get_flood_wait_seconds(exc)
            func_name = getattr(func, "__qualname__", getattr(func, "__name__", str(func)))
            if (
                attempts > max_retries
                or wait_seconds > max_wait_seconds
                or (
                    max_total_wait_seconds is not None
                    and (total_slept + wait_seconds) > max_total_wait_seconds
                )
            ):
                logger.warning(
                    "Telegram flood wait (%s: %ss) on %s (attempt %d/%d, total slept %.1fs, per-wait limit %.1fs) exceeded limits; raising",
                    type(exc).__name__,
                    wait_seconds,
                    func_name,
                    attempts,
                    max_retries,
                    total_slept,
                    max_wait_seconds,
                )
                raise
            logger.warning(
                "Telegram flood wait (%s: %ss) on %s (attempt %d/%d); sleeping %ss before retry",
                type(exc).__name__,
                wait_seconds,
                func_name,
                attempts,
                max_retries,
                wait_seconds,
            )
            if on_flood is not None:
                try:
                    try:
                        res = on_flood(wait_seconds, attempts)
                    except TypeError:
                        res = on_flood(wait_seconds)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    logger.debug("on_flood callback failed", exc_info=True)
            await sleeper(wait_seconds)
            total_slept += wait_seconds
            if on_flood_cleared is not None:
                try:
                    res = on_flood_cleared()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    logger.debug("on_flood_cleared callback failed", exc_info=True)


# Alias for readability
with_flood_retry = call_with_flood_retry
