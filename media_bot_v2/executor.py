"""Dedicated thread pool for the pipeline's blocking work (download, mp3
conversion, ffprobe, splitting, and reading upload parts off disk).

`asyncio.to_thread()` schedules onto the event loop's *default* executor,
which Python sizes to `min(32, (os.cpu_count() or 1) + 4)` unless something
calls `loop.set_default_executor()` first - 10 threads on a 6-core box,
regardless of `WORKERS`/`USER_WORKERS`. Every blocking call in the pipeline
went through that one shared pool, so raising `WORKERS` alone never raised
real concurrency: the 11th blocking call always queued behind the first 10.

This module gives that work its own executor, sized by `THREAD_POOL_SIZE`,
so that setting - not Python's CPU-derived default - is the real ceiling.
`main()` calls `init_thread_pool()` once at startup and `shutdown_thread_pool()`
once on exit; `get_thread_pool()` lazily creates a default-sized pool for
callers (mainly tests) that use the pipeline without going through bootstrap.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

DEFAULT_THREAD_POOL_SIZE = 48

_executor: ThreadPoolExecutor | None = None


def init_thread_pool(max_workers: int = DEFAULT_THREAD_POOL_SIZE) -> ThreadPoolExecutor:
    """Create the shared executor. Call once at startup, before any pipeline
    work runs. A second call (only expected in tests) replaces the pool,
    shutting down the old one without waiting for in-flight work."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
    _executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="media-bot-worker")
    return _executor


def shutdown_thread_pool(*, wait: bool = True) -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=wait)
        _executor = None


def get_thread_pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=DEFAULT_THREAD_POOL_SIZE, thread_name_prefix="media-bot-worker"
        )
    return _executor


async def run_in_thread[**P, T](func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """`asyncio.to_thread()`, but on the dedicated pool above instead of the
    event loop's default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_thread_pool(), functools.partial(func, *args, **kwargs))
