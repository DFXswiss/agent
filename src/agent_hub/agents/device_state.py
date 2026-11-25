"""The section `orchestrators` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4; ADR-082 section 6):
the orchestrator work of the device's sessions, so that a runner that missed the hint `session.orchestrator` catches
up at its reconcile - `{session_id, step, pairing_id}` with `step` `keys` (the task owner asked; the runner draws the
keys in the VM and registers them) or `handover` (the browser stored the handover of the confirmed pairing; the runner
fetches it with `GET /v1/device/sessions/{id}/orchestrator`), oldest session first (`agents.orchestrator.pending`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.agents.orchestrator import pending
from agent_hub.device.state import Section, StateRequest


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `orchestrators` for the device of `request`."""
    return {"orchestrators": pending(request.connection, request.device_id, request.now)}


SECTION = Section(fields=("orchestrators",), provide=provide)
