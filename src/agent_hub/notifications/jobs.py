"""Jobs of the notifications area (ADR-027 section 4): bind the hub context for the notification subscribers.

`notifications.bind_context` runs once right after the process starts its jobs and binds the context whose pool the
subscribers of `agent_hub.notifications.signals` use; later runs (hourly) only bind it again.
"""

from __future__ import annotations

from typing import Any

from agent_hub.jobs import Job
from agent_hub.notifications import signals

BIND_INTERVAL_SECONDS = 3600.0


def bind_context(context: Any) -> None:
    """Bind the context of this process for the notification subscribers."""
    signals.bind(context)


JOBS = (Job("notifications.bind_context", BIND_INTERVAL_SECONDS, bind_context),)
