from __future__ import annotations

import asyncio

import pytest

from xiaoheihe.task_manager import TaskManager


@pytest.mark.parametrize("method, key", [("start", "same"), ("start_profile_poller", "p")])
async def test_named_task_reuse_cancel_and_close(method, key) -> None:
    manager = TaskManager()
    gate = asyncio.Event()
    start = getattr(manager, method)

    async def worker():
        await gate.wait()

    first = await start(key, worker())
    unused = worker()
    second = await start(key, unused)
    assert first is second
    assert unused.cr_frame is None
    await manager.cancel(first.get_name())
    assert first.get_name() not in manager.task_names()
    restarted = await start(key, worker())
    assert restarted is not first
    await manager.close()
    await manager.close()
    late = worker()
    with pytest.raises(RuntimeError, match="关闭"):
        await start(key, late)
    assert late.cr_frame is None


async def test_sse_queue_is_bounded_and_closes() -> None:
    manager = TaskManager()
    iterator = manager.sse_events(max_queue=2)
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    manager.publish_sse({"type": "one"})
    assert (await pending)["type"] == "one"
    await iterator.aclose()
    await manager.close()


async def test_single_poller_per_profile() -> None:
    manager = TaskManager()
    gate = asyncio.Event()

    async def poller():
        await gate.wait()

    first, second, other = await asyncio.gather(
        manager.start_profile_poller("p", poller()),
        manager.start_profile_poller("p", poller()),
        manager.start_profile_poller("other", poller()),
    )
    assert first is second
    assert other is not first
    assert manager.task_names() == ["xhh-poll-other", "xhh-poll-p"]
    await manager.close()


async def test_task_failure_is_consumed_and_reported() -> None:
    manager = TaskManager()

    async def broken():
        raise RuntimeError("Authorization: Bearer secret-token-value")

    task = await manager.start("broken", broken())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    failures = manager.failures()
    assert failures[0]["task"] == "broken"
    assert "secret-token-value" not in failures[0]["error"]
    await manager.close()
