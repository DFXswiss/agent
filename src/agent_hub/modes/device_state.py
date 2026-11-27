"""The section `task_modes` of `GET /v1/device/state` (ADR-098 sections 3 and 4; extension point
`<area>/device_state.py:SECTION`).

`{"api": 1, "tasks": {task_id: [{mode, version, chosen_by, way, changed_at, driver}]}}`: the modes that are on for
every task with a session placed on the device that has not ended, in a workgroup the device is assigned to, each
with the driver state last reported (`driver`, null before the first report). The runner gives the session the brief
of each mode and runs its drivers; a device that takes a task over goes on from the reported state (ADR-087). A task
without a mode is not listed. The section's presence tells a runner that this hub knows modes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.crypto.encoding import format_time
from agent_hub.device.state import Section, StateRequest

API = 1


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `task_modes` for the device of `request` (module docstring)."""
    wgs = [wg for _org, wg in request.view.assigned()]
    rows = request.connection.execute(
        "SELECT m.task_id, m.mode, m.version, m.chosen_by, m.way, m.changed_at, m.driver_state FROM hub.task_mode m"
        " WHERE m.state = 'on' AND m.task_id IN (SELECT s.task_id FROM hub.session s WHERE s.device_id = %s"
        " AND s.state NOT IN ('stopped', 'failed') AND s.wg_id = ANY(%s::uuid[])) ORDER BY m.task_id, m.mode",
        (request.device_id, wgs),
    ).fetchall()
    tasks: dict[str, list[dict[str, Any]]] = {}
    for task_id, mode, version, chosen_by, way, changed_at, driver in rows:
        tasks.setdefault(str(task_id), []).append(
            {
                "mode": mode,
                "version": version,
                "chosen_by": str(chosen_by),
                "way": way,
                "changed_at": format_time(changed_at),
                "driver": driver,
            }
        )
    return {"task_modes": {"api": API, "tasks": tasks}}


SECTION = Section(("task_modes",), provide)
