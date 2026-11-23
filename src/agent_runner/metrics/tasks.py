"""Daemon task `metrics` (05-runner.md sections 1.1 and 14, 08-monitoring.md section 2, ADR-027 section 4).

Every `INTERVAL_S` (15 s, the scrape interval of the existing stack) one `Sampler.tick()`; the next round starts 15 s
after the previous one started (a slow round shortens the wait, never below 0). A round that fails is logged
(`runner.metrics sample_failed error=<type>`, never a value) and the loop goes on: sampling never stops because one
probe, the store or a sink failed, and it runs independently of the hub connection.

The sampler is built once per daemon context (`sampler_for`); tests set `metrics_options` (`MetricsOptions`) on the
context to inject the probe tools and the wall clock.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from agent_runner.metrics.probes import Tools
from agent_runner.metrics.sampler import Sampler
from agent_runner.registry import TaskSpec

INTERVAL_S = 15.0
SAMPLER_KEY = "_metrics_sampler"


@dataclass
class MetricsOptions:
    tools: Tools = field(default_factory=Tools)
    wall: Callable[[], float] = time.time


def sampler_for(context: Any) -> Sampler:
    state = vars(context)
    existing = state.get(SAMPLER_KEY)
    if existing is None:
        options = state.get("metrics_options")
        chosen = options if isinstance(options, MetricsOptions) else MetricsOptions()
        existing = Sampler(context, chosen.tools, chosen.wall)
        state[SAMPLER_KEY] = existing
    return cast(Sampler, existing)


async def run(context: Any) -> None:
    sampler = sampler_for(context)
    clock, sleep = context.host.clock, context.host.sleep
    while not context.stopping.is_set():
        started = clock()
        try:
            await sampler.tick()
        except Exception as exc:
            sampler.line(f"sample_failed error={type(exc).__name__}")
        await sleep(max(INTERVAL_S - (clock() - started), 0.0))


TASKS = [TaskSpec("metrics", run)]
