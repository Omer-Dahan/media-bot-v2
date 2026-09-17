"""ConcurrencyLimiter: per-user + global asyncio.Semaphore gating, with a
guaranteed release on every exit path (success, exception, cancellation) and
an on_wait hook so a queued user gets told they're waiting."""

import asyncio

import pytest

from media_bot_v2.queue.limiter import ConcurrencyLimiter


async def test_slot_allows_immediate_entry_when_under_limit():
    limiter = ConcurrencyLimiter(global_limit=2, per_user_limit=2)
    entered = False
    async with limiter.slot(user_id=1):
        entered = True
    assert entered


async def test_global_limit_blocks_a_second_concurrent_user():
    limiter = ConcurrencyLimiter(global_limit=1, per_user_limit=5)
    order: list[str] = []
    first_holding = asyncio.Event()
    release_first = asyncio.Event()

    async def holder():
        async with limiter.slot(user_id=1):
            order.append("first-in")
            first_holding.set()
            await release_first.wait()
            order.append("first-out")

    async def waiter():
        await first_holding.wait()
        async with limiter.slot(user_id=2):
            order.append("second-in")

    task1 = asyncio.create_task(holder())
    task2 = asyncio.create_task(waiter())
    await first_holding.wait()
    await asyncio.sleep(0.01)  # let task2 block on the global semaphore
    assert order == ["first-in"]

    release_first.set()
    await asyncio.gather(task1, task2)
    assert order == ["first-in", "first-out", "second-in"]


async def test_per_user_limit_blocks_the_same_user_but_not_others():
    limiter = ConcurrencyLimiter(global_limit=5, per_user_limit=1)
    order: list[str] = []
    first_holding = asyncio.Event()
    release_first = asyncio.Event()

    async def same_user_second_call():
        await first_holding.wait()
        async with limiter.slot(user_id=1):
            order.append("same-user-second")

    async def other_user_call():
        await first_holding.wait()
        async with limiter.slot(user_id=2):
            order.append("other-user")

    async def first_call():
        async with limiter.slot(user_id=1):
            order.append("same-user-first")
            first_holding.set()
            await release_first.wait()

    t1 = asyncio.create_task(first_call())
    t2 = asyncio.create_task(same_user_second_call())
    t3 = asyncio.create_task(other_user_call())
    await first_holding.wait()
    await asyncio.sleep(0.01)
    assert "other-user" in order  # a different user isn't blocked by user 1's slot
    assert "same-user-second" not in order  # same user's second call is still queued

    release_first.set()
    await asyncio.gather(t1, t2, t3)
    assert "same-user-second" in order


async def test_on_wait_called_only_when_a_cap_is_already_exhausted():
    limiter = ConcurrencyLimiter(global_limit=1, per_user_limit=5)
    waited = []

    async def on_wait():
        waited.append(True)

    async with limiter.slot(user_id=1, on_wait=on_wait):
        pass
    assert waited == []  # slot was free, no need to warn the user

    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with limiter.slot(user_id=1):
            holding.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await holding.wait()

    async def second():
        async with limiter.slot(user_id=2, on_wait=on_wait):
            pass

    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.01)
    assert waited == [True]  # global slot was taken, on_wait fired before blocking

    release.set()
    await asyncio.gather(task, second_task)


async def test_slot_releases_both_semaphores_on_exception():
    limiter = ConcurrencyLimiter(global_limit=1, per_user_limit=1)

    with pytest.raises(RuntimeError):
        async with limiter.slot(user_id=1):
            raise RuntimeError("boom")

    # Both semaphores must be free again - a second acquire must not hang.
    async with asyncio.timeout(1):
        async with limiter.slot(user_id=1):
            pass


async def test_slot_releases_both_semaphores_on_cancellation():
    limiter = ConcurrencyLimiter(global_limit=1, per_user_limit=1)
    started = asyncio.Event()

    async def holder():
        async with limiter.slot(user_id=1):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(holder())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with asyncio.timeout(1):
        async with limiter.slot(user_id=1):
            pass
