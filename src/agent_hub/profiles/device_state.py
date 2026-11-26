"""The section `agent_profiles` of `GET /v1/device/state` (ADR-093 section 3; extension point
`<area>/device_state.py:SECTION`).

`{"api": 1, "sessions": {session_id: profile|null}, "workgroups": {wg_id: {name: profile}}}` with `profile` =
`{name, operations, reads}`:
- `sessions`: the effective profile of every session placed on the device that has not ended, in a workgroup the device
  is assigned to (`agent_hub.profiles.store.effective`: the task's and every snapshot along the chain of parents; null
  = every right). The runner checks every request, work VM and browser call and read of the session against it.
- `workgroups`: for each assigned workgroup, what every usable profile name resolves to (built-in roles, the
  organization's and the workgroup's own), so the runner can check the profile of a `subagent.request` before it
  registers it (the hub decides bindingly, H2b).

The section's presence tells a runner that this hub knows profiles; a runner without it (an older hub) knows none.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.profiles import store

API = 1


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `agent_profiles` for the device of `request` (module docstring)."""
    wgs = [wg for _org, wg in request.view.assigned()]
    rows = request.connection.execute(
        "SELECT id FROM hub.session WHERE device_id = %s AND state NOT IN ('stopped', 'failed')"
        " AND wg_id = ANY(%s::uuid[]) ORDER BY created_at, id",
        (request.device_id, wgs),
    ).fetchall()
    sessions: dict[str, Any] = {}
    for (session_id,) in rows:
        found = store.effective(request.connection, session_id)
        sessions[str(session_id)] = None if found is None else found.as_json()
    workgroups = {
        wg: {
            name: item.profile.as_json()
            for name, item in sorted(store.definitions(request.connection, org, wg).items())
        }
        for org, wg in request.view.assigned()
    }
    return {"agent_profiles": {"api": API, "sessions": sessions, "workgroups": workgroups}}


SECTION = Section(("agent_profiles",), provide)
