"""The settle helper of the runner test kits drains a task registry whose entries leave in a done callback."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from .settle import settle

GUARD_S = 10.0


def bounded(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run `main` on its own loop in a daemon thread: a loop that never yields cannot time itself out."""
    failure: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(main())
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(GUARD_S)
    assert not thread.is_alive(), f"the settle loop did not return within {GUARD_S} s"
    if failure:
        raise failure[0]


async def done_but_listed() -> set[asyncio.Task[None]]:
    """A registry holding one task that is done while its removing done callback is still queued."""
    tasks: set[asyncio.Task[None]] = set()

    async def work() -> None:
        return None

    task = asyncio.ensure_future(work())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    # one loop turn: the task runs to its end and queues `discard` behind this coroutine's own resumption
    await asyncio.sleep(0)
    assert task.done()
    assert task in tasks
    return tasks


def test_a_done_task_whose_removal_is_still_queued_settles() -> None:
    async def main() -> None:
        tasks = await done_but_listed()
        await settle(lambda: tasks, "queries")
        assert tasks == set()

    bounded(main)


def test_tasks_added_while_settling_are_awaited_too() -> None:
    async def main() -> None:
        tasks: set[asyncio.Task[None]] = set()
        finished: list[str] = []

        def spawn(name: str, then: Callable[[], None] | None = None) -> None:
            async def work() -> None:
                await asyncio.sleep(0)
                finished.append(name)
                if then is not None:
                    then()

            task = asyncio.ensure_future(work())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        spawn("first", lambda: spawn("second"))
        await settle(lambda: tasks, "queries")
        assert finished == ["first", "second"]
        assert tasks == set()

    bounded(main)


def test_an_entry_without_a_task_yet_is_waited_for() -> None:
    async def main() -> None:
        flows: dict[str, asyncio.Task[None] | None] = {"a": None}

        async def clear() -> None:
            flows.clear()

        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(clear()))
        await settle(lambda: list(flows.values()), "flows")
        assert flows == {}

    bounded(main)


def test_a_registry_that_never_drains_fails_with_a_clear_message() -> None:
    async def main() -> None:
        stuck = asyncio.get_running_loop().create_future()
        with pytest.raises(AssertionError, match=r"ending did not settle within 0\.05 s"):
            await settle(lambda: [stuck], "ending", limit=0.05)
        stuck.cancel()

    bounded(main)
