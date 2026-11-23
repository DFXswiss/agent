"""The section `pings` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4): the undelivered
`session_ping` rows of the sessions that run on the device and have not ended, oldest first, as
`{ping_id, session_id, kind, level, ref, created_at}` (`agent_hub.notifications.pings.undelivered`). The runner delivers
each to its session and reports `ping.delivered` through the outbox, which marks the row delivered."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.notifications.pings import undelivered


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `pings` for the device of `request`."""
    return {"pings": undelivered(request.connection, request.device_id)}


SECTION = Section(fields=("pings",), provide=provide)
