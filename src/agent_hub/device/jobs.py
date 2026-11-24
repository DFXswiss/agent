"""Jobs of the device area (ADR-027 section 4): bind the hub context for the reconcile hints.

`device.bind_context` runs once right after the process starts its jobs and binds the context whose pool the
`log.appended` subscriber reads (`agent_hub.device.signals`); later runs (hourly) only bind it again.
"""

from __future__ import annotations

from typing import Any

from agent_hub.device import signals
from agent_hub.jobs import Job

BIND_INTERVAL_SECONDS = 3600.0


def bind_context(context: Any) -> None:
    """Bind the context of this process for the reconcile subscriber."""
    signals.bind(context)


JOBS = (Job("device.bind_context", BIND_INTERVAL_SECONDS, bind_context),)
