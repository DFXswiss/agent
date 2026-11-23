"""Wait until a set of background tasks has drained (the settle helpers of the runner test kits).

A task removes itself from its registry in a done callback (`task.add_done_callback(tasks.discard)`), which the event
loop runs only at its next turn. Since Python 3.12 `asyncio.gather` over futures that are all done completes eagerly,
so `while tasks: await asyncio.gather(*tasks)` never gives the loop a turn once every listed task is done and its
callback is still queued: the loop spins at full CPU and never returns (docs/platform/decisions/HOT-01.md).
`asyncio.wait` always waits for its own completion callback, which the loop schedules after the earlier removal
callbacks, so the registry has shrunk when it returns. The bound turns any other non-draining registry into a clear
failure instead of a hang.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Collection
from typing import Any

SETTLE_LIMIT_S = 60.0
UNTIL_PAUSE_S = 0.005


async def settle(
    tasks: Callable[[], Collection[asyncio.Future[Any] | None]], what: str, *, limit: float = SETTLE_LIMIT_S
) -> None:
    """Return once `tasks()` is empty; fail after `limit` seconds of real time with the tasks still listed."""
    try:
        async with asyncio.timeout(limit):
            while listed := tasks():
                running = [task for task in listed if task is not None]
                if running:
                    await asyncio.wait(running)
                else:
                    await asyncio.sleep(0)
    except TimeoutError:
        raise AssertionError(f"{what} did not settle within {limit} s: {list(tasks())}") from None


async def until(condition: Callable[[], bool | Awaitable[bool]], what: str, *, limit: float = SETTLE_LIMIT_S) -> None:
    """Return once `condition()` (plain or awaitable) is true, polling with a short pause that yields the loop; fail
    after `limit` seconds of real time. For states that background tasks reach (a database row, a log line), instead
    of a fixed sleep that a loaded device can outlast."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    while True:
        result = condition()
        if await result if inspect.isawaitable(result) else result:
            return
        if loop.time() > deadline:
            raise AssertionError(f"{what} not reached within {limit} s")
        await asyncio.sleep(UNTIL_PAUSE_S)
