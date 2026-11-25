"""The device state's `failovers` (ADR-086 section 3; contracts/api/device-state.response.json): the live main
sessions of this device that continue a session lost with a failed device (`from_session`), with the number of the lost
session's test runs the hub cancelled. Read by the context restore (no wait for the lost session's final checkpoint,
the failover note) and the prompt routing (the interrupted instruction, accepted by the lost session, is delivered
once more to its successor). An old hub sends no `failovers`; a malformed entry is ignored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Failover:
    """The `failovers` entry of a session that continues one of a failed device."""

    from_session: str
    cancelled_runs: int


def failover_of(desired: Mapping[str, Any] | None, session_id: str) -> Failover | None:
    """The `failovers` entry of the session in the last desired state; None when it has none or it is malformed."""
    found = None if desired is None else desired.get("failovers")
    for item in found if isinstance(found, list) else ():
        if not isinstance(item, dict) or item.get("session_id") != session_id:
            continue
        source, cancelled = item.get("from_session"), item.get("cancelled_runs")
        if isinstance(source, str) and type(cancelled) is int and cancelled >= 0:
            return Failover(from_session=source, cancelled_runs=cancelled)
    return None
