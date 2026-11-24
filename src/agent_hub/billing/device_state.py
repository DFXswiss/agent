"""The section `billing` of `GET /v1/device/state` (12-billing.md section 5, ADR-062 section 6; ADR-027 section 4
`<area>/device_state.py:SECTION`).

`{"api": 1, "active": bool, "orgs": {<org_id>: {"exhausted": bool}}}` for every organization with a workgroup assigned
to the device. `api` tells a runner that this hub accepts `runtime` items and the billing fields of `usage` items;
`active` that a price list is in force. The provider also binds the start credit (`agent_hub.billing.binding`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.billing import binding, credit
from agent_hub.device.state import Section, StateRequest

API = 1


def provide(request: StateRequest) -> Mapping[str, Any]:
    binding.bind(request.config)
    orgs = sorted({str(org) for org, _wg in request.view.assigned()})
    active = credit.active(request.connection, request.now)
    states = credit.exhausted_orgs(request.connection, orgs, request.now)
    return {
        "billing": {
            "api": API,
            "active": active,
            "orgs": {org: {"exhausted": states[org]} for org in orgs},
        }
    }


SECTION = Section(("billing",), provide)
