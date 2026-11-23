"""Retention of the verified prompt events (KOR-03, AUD-01 B-19; RUN-05 decision 25).

`runner.prompt_event` keeps every prompt of the device's workgroups as received (ciphertext and signature). A prompt
whose delivery is final (`accepted`, `refused`, `failed`) and that did not change for `RETAIN_S` is not needed any
more: its delivery row stays (it alone guarantees that the prompt is never delivered again, also when the hub routes
it once more), only the stored event goes. Kept regardless:

- prompts without a delivery row or with an open one (they may still be routed or delivered);
- per task the earliest accepted prompt: it tells a new main session of the task that it continues earlier work
  (`queue.earlier_of_task`, the restart hint of `resume.py`); an accepted delivery never changes again, so the kept
  prompt stays the same from round to round (prompts without delivery or with an open one are kept anyway).

A routing of a pruned final prompt answers its final state (`Deliverer.route_one`), not `missing`.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

RETAIN_S = 24 * 3600
PRUNE_BATCH = 5000


async def prune(conn: AsyncConnection[Any], *, retain_s: float = RETAIN_S, batch: int = PRUNE_BATCH) -> int:
    """Delete up to `batch` delivered prompt events older than `retain_s` (module docstring); returns the number."""
    cur = await conn.execute(
        "DELETE FROM runner.prompt_event WHERE event_id IN (SELECT e.event_id FROM runner.prompt_event e"
        " JOIN runner.prompt_delivery d ON d.event_id = e.event_id"
        " WHERE d.state IN ('accepted', 'refused', 'failed')"
        " AND d.updated_at < now() - make_interval(secs => %s)"
        " AND e.received_at < now() - make_interval(secs => %s)"
        " AND e.scope_seq <> COALESCE((SELECT min(f.scope_seq) FROM runner.prompt_event f"
        " JOIN runner.prompt_delivery g ON g.event_id = f.event_id WHERE f.task_id = e.task_id"
        " AND g.state = 'accepted'), -1)"
        " ORDER BY e.received_at LIMIT %s)",
        (retain_s, retain_s, batch),
    )
    return cur.rowcount
