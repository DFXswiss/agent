"""The section `vm_switch` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4; ADR-093 section 5): the
live workgroup VMs of the device a member asked to switch to the newest golden now, oldest request first
(`service.requested`). The field is always present, so a runner knows this hub reads `upgrade` and `replaced_by`;
`vm_upgrade_version` (always `UPGRADE_VERSION`) tells it which hold reasons this hub reads (2: `checkpoint_impossible`
and `checkpoint_at`, ADR-093 addendum HOT-24), and `vm_switch_at` gives the time of each request: a request covers
only the `checkpoint_impossible` holds the device had seen by then (HOT-24)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.crypto.encoding import format_time
from agent_hub.device.state import Section, StateRequest
from agent_hub.vmswitch.service import requested

UPGRADE_VERSION = 2


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The fields `vm_switch`, `vm_switch_at` and `vm_upgrade_version` for the device of `request`."""
    found = requested(request.connection, request.device_id)
    return {
        "vm_switch": [vm for vm, _ in found],
        "vm_switch_at": {vm: format_time(at) for vm, at in found},
        "vm_upgrade_version": UPGRADE_VERSION,
    }


SECTION = Section(fields=("vm_switch", "vm_switch_at", "vm_upgrade_version"), provide=provide)
