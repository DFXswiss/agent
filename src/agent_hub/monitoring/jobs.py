"""Background jobs of the monitoring area (ADR-027 section 4): the 5-minute rollup with the retention
(`agent_hub.monitoring.rollup`) and the `metrics` frames every 15 s (`agent_hub.monitoring.stream`)."""

from __future__ import annotations

from typing import Any

from agent_hub.jobs import Job
from agent_hub.monitoring.rollup import BUCKET_SECONDS, RollupPass, roll_up
from agent_hub.monitoring.stream import FRAME_SECONDS, publish_metrics


def run_rollup(context: Any) -> RollupPass:
    """One rollup and retention pass on the hub clock."""
    with context.pool.connection() as connection:
        return roll_up(connection, context.clock())


JOBS = [
    Job("monitoring.rollup", float(BUCKET_SECONDS), run_rollup, initial_delay_seconds=30.0),
    Job("monitoring.metrics_frames", FRAME_SECONDS, publish_metrics, initial_delay_seconds=FRAME_SECONDS),
]
