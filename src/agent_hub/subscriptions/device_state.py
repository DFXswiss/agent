"""The section `subscriptions` of `GET /v1/device/state` (03-api.md section 6.1, ADR-090; ADR-027 section 4
`<area>/device_state.py:SECTION`): `{api: 2, shared}`. `api` tells the runner that this hub accepts
`quota.subscription_id` and the subscription endpoints of section 6.1 (1) and `usage.subscription_id` (2, ADR-100),
so a runner sends them only to such a hub;
`shared` lists the further subscriptions this device may be asked to use (`store.shared_for`: their mirrored use names
an organization the device is assigned in, or the operator's pool for a platform device, and the device holds the
owner's `CK`). The runner uses one only when the sealed `use` of the record allows the session's organization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.subscriptions.store import shared_for

API = 2


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field of this area."""
    orgs = sorted({org for org, _wg in request.view.assigned()})
    row = request.connection.execute("SELECT kind FROM hub.device WHERE id = %s", (request.device_id,)).fetchone()
    platform = row is not None and row[0] == "platform"
    return {"subscriptions": {"api": API, "shared": shared_for(request.connection, request.device_id, orgs, platform)}}


SECTION = Section(("subscriptions",), provide)
