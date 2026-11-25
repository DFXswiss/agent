"""Daemon task `retention` (KOR-03, AUD-01 B-19): bounds the runner store on a long-running device.

Every `INTERVAL_S` (and once after the start) it deletes, in batches of its own transactions:

- acknowledged outbox items older than an hour (`sync.outbox.prune`; ADR-038: the event items of the last hour stay
  for the subagent results; the newest item always stays for the numbering);
- prompt events whose delivery is final for a day (`prompts.retention.prune`; delivery rows stay);
- merge preparations and workspace digests older than seven days (`github.merge_prep.prune`, KOR-48).

Other tables bound themselves (`runner.ping`: rows of sessions ended an hour ago, RUN-14) or are small per object
(profiles, settings, sessions). A failing round is logged (`runner.retention failed error=<type>`) and tried again at
the next interval.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_runner.github import merge_prep
from agent_runner.prompts import retention as prompt_retention
from agent_runner.registry import TaskSpec
from agent_runner.sync import outbox

INTERVAL_S = 600.0
Prune = Callable[[Any], Awaitable[int]]
PRUNERS: tuple[tuple[str, Prune, int], ...] = (
    ("outbox", outbox.prune, outbox.PRUNE_BATCH),
    ("prompt_event", prompt_retention.prune, prompt_retention.PRUNE_BATCH),
    ("merge_prep", merge_prep.prune, merge_prep.PRUNE_BATCH),
)


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.retention {text}\n")
    context.log.flush()


async def prune_all(context: Any) -> dict[str, int]:
    """One round: each table in batches until a batch deletes less than a full one; returns the counts."""
    counts: dict[str, int] = {}
    for name, prune, batch in PRUNERS:
        total = 0
        while True:
            async with context.pool.connection() as conn, conn.transaction():
                deleted = await prune(conn)
            total += deleted
            if deleted < batch:
                break
        counts[name] = total
    return counts


async def run_retention(context: Any) -> None:
    while not context.stopping.is_set():
        try:
            counts = await prune_all(context)
        except Exception as exc:
            _line(context, f"failed error={type(exc).__name__}")
        else:
            if any(counts.values()):
                _line(context, " ".join(f"{name}={count}" for name, count in counts.items()))
        await context.host.sleep(INTERVAL_S)


TASKS = [TaskSpec("retention", run_retention)]
