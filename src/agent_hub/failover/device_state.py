"""The fields `failover_seconds` and `failovers` of `GET /v1/device/state` (03-api.md section 6.1, ADR-086 sections 3
and 4; ADR-027 section 4: `<area>/device_state.py:SECTION`).

- `failover_seconds`: `AGENT_HUB_DEVICE_FAILOVER_SECONDS`, after which the hub moves the sessions of a device that
  stays away (information for the runner and its operators).
- `failovers`: for every live main session of the device that the hub created as the successor of a session on a
  failed device (`session.failover_of`), oldest first: `{session_id, from_session, cancelled_runs}` -
  `cancelled_runs` counts the test runs of the lost session the hub ended with `device_failed`. The runner does not
  wait for a final checkpoint of `from_session` (it will not come) and says in the session notice that the session
  continues after a device failure. Old runners ignore the fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.sessions.derive import DEVICE_FAILED


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The two fields for the device of `request` (module docstring)."""
    rows = request.connection.execute(
        "SELECT s.id, s.failover_of, s.wg_id, (SELECT count(*) FROM hub.operation o WHERE o.session_id = s.failover_of"
        " AND o.type = 'test.run' AND o.state = 'failed' AND o.error_code = %s) FROM hub.session s"
        " WHERE s.device_id = %s AND s.depth = 0 AND s.failover_of IS NOT NULL AND s.state NOT IN ('stopped', 'failed')"
        " ORDER BY s.created_at, s.id",
        (DEVICE_FAILED, request.device_id),
    ).fetchall()
    return {
        "failover_seconds": request.config.device_failover_seconds,
        "failovers": [
            {"session_id": str(row[0]), "from_session": str(row[1]), "cancelled_runs": int(row[3])}
            for row in rows
            if request.view.reads_wg(row[2])
        ],
    }


SECTION = Section(fields=("failover_seconds", "failovers"), provide=provide)
