"""Jobs of the usage area (ADR-027 section 4): bind the hub configuration for the budget hook and the outbox handlers.

`usage.bind_config` runs at start, before the hub serves its first request (`at_start`, KOR-06), and binds the settings
the budget hook and the `quota` handler need (`agent_hub.usage.binding`); later runs (hourly, with the periodic jobs)
only bind them again.
"""

from __future__ import annotations

from typing import Any

from agent_hub.jobs import Job
from agent_hub.usage import binding

BIND_INTERVAL_SECONDS = 3600.0


def bind_config(context: Any) -> None:
    """Bind the configuration of this process."""
    binding.bind(context.config)


JOBS = (
    Job(
        "usage.bind_config",
        BIND_INTERVAL_SECONDS,
        bind_config,
        initial_delay_seconds=BIND_INTERVAL_SECONDS,
        at_start=True,
    ),
)
