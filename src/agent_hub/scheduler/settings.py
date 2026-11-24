"""The scheduler settings of the running hub for the code paths that get no configuration (ADR-075, ADR-049).

The re-scheduling of follow-ups and refusal attempts (`agent_hub.sessions.prompts`, `agent_hub.sessions.retry`) runs
inside the device and outbox handlers, which receive the transaction but not the hub configuration. `bind(config)`
keeps `AGENT_HUB_VMS_PER_WG`, `AGENT_HUB_CAPACITY_REFUSALS` and `AGENT_HUB_REFUSAL_HOLD_SECONDS` for the process:
the job `scheduler.reschedule` binds them at every run (also once before the first request) and the task routes bind
them on every request. Before the first binding the defaults apply (they only make the scheduler more generous).
"""

from __future__ import annotations

import threading
from typing import Any

from agent_hub.config import (
    DEFAULT_CAPACITY_REFUSALS,
    DEFAULT_REFUSAL_HOLD_SECONDS,
    DEFAULT_VMS_PER_WG,
    HubConfig,
)

_lock = threading.Lock()
_state: dict[str, Any] = {}


def bind(config: HubConfig) -> None:
    """Keep the settings of the running hub (module docstring)."""
    with _lock:
        _state["vms_per_wg"] = config.vms_per_wg
        _state["refusals"] = config.capacity_refusals
        _state["hold"] = config.refusal_hold_seconds


def unbind() -> None:
    """Forget the bound settings (tests)."""
    with _lock:
        _state.clear()


def vms_per_wg() -> int | None:
    """`AGENT_HUB_VMS_PER_WG`; None for no limit (0)."""
    found: int = _state.get("vms_per_wg", DEFAULT_VMS_PER_WG)
    return found or None


def capacity_refusals() -> int:
    """`AGENT_HUB_CAPACITY_REFUSALS`."""
    found: int = _state.get("refusals", DEFAULT_CAPACITY_REFUSALS)
    return found


def refusal_hold_seconds() -> int:
    """`AGENT_HUB_REFUSAL_HOLD_SECONDS`."""
    found: int = _state.get("hold", DEFAULT_REFUSAL_HOLD_SECONDS)
    return found
