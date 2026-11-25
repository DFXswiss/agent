"""Heartbeat field `sessions` of the sessions area (03-api.md section 6.2; `sync/extensions.py`; KOR-10, AUD-02 NA-09).

- `sessions`: `[{session_id, state}]` of the sessions on this device that have not ended (not `stopped`/`failed`),
  oldest first, all of them (ADR-088 section 3: `sync.ws.split_heartbeat` spreads a long list over several frames).

The hub checks the field (a version-7 UUID and a state of the closed list, else it closes the socket) and does not act
on it: the session states it shows come from the `session.status` events of the outbox. The field tells the hub which
sessions the device still runs. Only rows the hub accepts are sent (an ID that is no version-7 UUID is left out; the
query asks for the open states of `records.STATES` only), so one odd row in the store never cuts the device off.
"""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.ids import is_uuid7
from agent_runner.sessions import records
from agent_runner.sync.extensions import HeartbeatSection

OPEN_STATES = sorted(records.STATES - records.FINAL)


async def sessions(context: Any) -> list[dict[str, str]]:
    async with context.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id::text, state FROM runner.session WHERE state = ANY(%s) ORDER BY created_at, id", (OPEN_STATES,)
        )
        rows = await cur.fetchall()
    return [{"session_id": str(sid), "state": str(state)} for sid, state in rows if is_uuid7(sid)]


SECTIONS = [HeartbeatSection("sessions", sessions)]
