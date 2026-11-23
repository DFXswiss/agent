# HOT-01 - Endless loop in the settle helpers of the runner test kits

## Cause
`SKit.settle_queries` (tests/platform/runner/subagents/kit.py) waited with
`while controller.queries: await asyncio.gather(*controller.queries, return_exceptions=True)`. The controller removes
a finished read task from `queries` in a done callback (`task.add_done_callback(self.queries.discard)`), and the event
loop runs done callbacks only at its next turn (`call_soon`). Since Python 3.12 `asyncio.gather` completes eagerly
when every future it gets is already done: it returns a finished future, and awaiting a finished future does not give
the loop a turn. When the test coroutine resumed in the same loop turn in which the last read task finished - its
`discard` callback queued behind the coroutine - the loop saw the task still listed, gathered it, returned at once and
repeated forever at full CPU without ever letting `discard` run. Two ready handles landing in the same loop turn
depends on timing (the read does real database and guest I/O), which is why the hang appeared only under heavy load
and could not be reproduced by repetition. It is a test-kit defect; the controller is correct (its shutdown path
gathers once, not in a loop).

## Correction
- New helper `tests/platform/runner/settle.py:settle(tasks, what, limit=60)`: waits with `asyncio.wait`, which always
  awaits its own completion callback; the loop schedules that callback after the earlier removal callbacks, so the
  registry has shrunk when it returns. Entries without a task yet yield the loop with `asyncio.sleep(0)`. A real-time
  bound turns any registry that never drains into an `AssertionError` naming the tasks instead of a hang.
- `settle_queries`, `settle_ends` (subagents kit) and the three `flows` settle helpers (request service tests, github
  device-flow tests, login conftest) use it. The flows helpers had the same pattern (and would spin on an entry whose task is not set yet);
  their flows leave in the task itself, so they were not observed hanging.
- `tests/platform/runner/test_settle.py` builds the exact state deterministically (one loop turn after which a task is
  done and its `discard` is still queued) and runs each case on its own loop in a daemon thread with a 10 s guard,
  because a loop that never yields cannot time itself out. With the former `gather` loop the test fails after the
  guard (leaving the spinning thread behind in that worker - only while the test is red); with the helper it passes
  in milliseconds.
- Unlike `gather(..., return_exceptions=True)`, `asyncio.wait` does not mark a task exception as retrieved. The waited
  tasks catch their exceptions today; a future task that raises would now be logged by asyncio instead of being
  swallowed silently, which is the wanted direction.
