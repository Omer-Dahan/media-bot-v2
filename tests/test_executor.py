"""Dedicated thread pool (media_bot_v2/executor.py): the real concurrency
ceiling for the pipeline's blocking work (download, mp3 conversion, ffprobe,
split, upload part I/O).

Before this module existed, every one of those calls went through
`asyncio.to_thread()`, which schedules onto the event loop's *default*
executor - Python sizes that to `min(32, (os.cpu_count() or 1) + 4)`, 10
threads on this project's 6-core deployment box, regardless of
`WORKERS`/`USER_WORKERS`. These tests prove: (a) the pipeline's blocking
calls actually run on this dedicated pool, not the default one; (b) the
pool's own concurrency ceiling is exactly `THREAD_POOL_SIZE`, including
above Python's ~10-thread default; (c) the pool shuts down cleanly.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from media_bot_v2 import executor as executor_module
from media_bot_v2.executor import (
    get_thread_pool,
    init_thread_pool,
    run_in_thread,
    shutdown_thread_pool,
)


@pytest.fixture(autouse=True)
def _reset_executor_state():
    """Isolates each test from whatever pool a previous test (or a lazy
    get_thread_pool() call from pipeline code elsewhere in the suite) left
    behind, and cleans up whatever this test itself creates."""
    saved = executor_module._executor
    executor_module._executor = None
    yield
    if executor_module._executor is not None:
        executor_module._executor.shutdown(wait=False)
    executor_module._executor = saved


def _blocking(duration: float) -> str:
    time.sleep(duration)
    return threading.current_thread().name


async def test_run_in_thread_uses_the_dedicated_named_pool_not_pythons_default():
    """Proves the heavy-work helper actually runs on a media-bot-worker
    thread (our pool's `thread_name_prefix`), not asyncio's unnamed default
    executor - the concrete check for finding A's requirement (a)."""
    init_thread_pool(4)
    name = await run_in_thread(_blocking, 0.01)
    assert name.startswith("media-bot-worker")


async def test_thread_pool_size_is_the_real_concurrency_ceiling():
    """Finding A's requirement (b): with THREAD_POOL_SIZE=4, at most 4
    blocking tasks ever run at once, no matter how many are queued - and
    with THREAD_POOL_SIZE=16 (above Python's default executor's ~10-thread
    ceiling on this size box), 16 run concurrently. Before this module
    existed, both cases were silently capped at Python's default regardless
    of what WORKERS/USER_WORKERS said."""

    async def measure_max_concurrency(pool_size: int, task_count: int) -> int:
        init_thread_pool(pool_size)
        lock = threading.Lock()
        state = {"current": 0, "max": 0}

        def worker() -> None:
            with lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
            time.sleep(0.15)
            with lock:
                state["current"] -= 1

        await asyncio.gather(*(run_in_thread(worker) for _ in range(task_count)))
        return state["max"]

    assert await measure_max_concurrency(4, 12) == 4
    assert await measure_max_concurrency(16, 20) == 16


async def test_thread_pool_size_defaults_to_48_when_never_initialized():
    """Callers that use the pipeline without going through bootstrap's
    explicit init_thread_pool() (mainly tests) still get a usable pool,
    lazily sized at the same default as THREAD_POOL_SIZE's config default."""
    pool = get_thread_pool()
    assert pool._max_workers == executor_module.DEFAULT_THREAD_POOL_SIZE == 48


def test_shutdown_thread_pool_closes_the_executor_cleanly():
    pool = init_thread_pool(2)
    shutdown_thread_pool()
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    assert executor_module._executor is None


def test_shutdown_thread_pool_is_a_no_op_when_never_initialized():
    shutdown_thread_pool()  # must not raise
    assert executor_module._executor is None


def test_init_thread_pool_replacing_an_existing_pool_shuts_down_the_old_one():
    first = init_thread_pool(2)
    init_thread_pool(4)
    with pytest.raises(RuntimeError):
        first.submit(lambda: None)
