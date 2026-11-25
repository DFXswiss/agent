"""The section `vm_switch` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4; ADR-093 section 5): the
live workgroup VMs of the device a member asked to switch to the newest golden now, oldest request first
(`service.requested`). The field is always present, so a runner knows this hub reads `upgrade` and `replaced_by`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.vmswitch.service import requested


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `vm_switch` for the device of `request`."""
    return {"vm_switch": requested(request.connection, request.device_id)}


SECTION = Section(fields=("vm_switch",), provide=provide)
