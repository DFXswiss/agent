"""The job of the failover area (ADR-086, ADR-027 section 4 `<area>/jobs.py:JOBS`).

`failover.sweep` runs `agent_hub.failover.sweep.sweep` every `INTERVAL_SECONDS` in every hub process: it interrupts
the active sessions of failed devices, moves their tasks to another device when one can take them and clears the
interruption of sessions whose device came back. Concurrent sweeps of several processes skip the tasks another one
holds (`FOR UPDATE SKIP LOCKED`).
"""

from __future__ import annotations

from agent_hub.failover.sweep import sweep
from agent_hub.jobs import Job

INTERVAL_SECONDS = 15.0

JOBS = (Job("failover.sweep", INTERVAL_SECONDS, sweep, initial_delay_seconds=15.0),)
