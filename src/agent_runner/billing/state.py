"""What the hub says about billing, from the last desired state (`GET /v1/device/state` section `billing`,
12-billing.md section 5, ADR-062).

- `api(desired)`: the hub accepts `runtime` items and the billing fields of `usage` items (`billing.api >= 1`). An older
  hub has no `billing` section; the runner then sends neither (the hub would refuse them).
- `active(desired)`: a price list is in force.
- `exhausted(desired, org_id)`: the organization's credit is used up (only while active; an org the hub does not list
  is not exhausted).
- `origin(conn, session)`: whose model access a session uses (AUD-04 W10): `platform` (the platform key; also a
  session whose access is not recorded yet, as for the cost basis), `operator` (bound to an operator subscription,
  ADR-052; `runner.operator_origin` keeps it after the binding is pruned) or `own` (the user's own subscription or API
  key).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions.records import LocalSession


def section(desired: Mapping[str, Any] | None) -> Mapping[str, Any]:
    found = None if desired is None else desired.get("billing")
    return found if isinstance(found, Mapping) else {}


def api(desired: Mapping[str, Any] | None) -> bool:
    value = section(desired).get("api")
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def active(desired: Mapping[str, Any] | None) -> bool:
    return api(desired) and section(desired).get("active") is True


def exhausted(desired: Mapping[str, Any] | None, org_id: str) -> bool:
    if not active(desired):
        return False
    orgs = section(desired).get("orgs")
    entry = orgs.get(org_id) if isinstance(orgs, Mapping) else None
    return isinstance(entry, Mapping) and entry.get("exhausted") is True


async def origin(conn: AsyncConnection[Any], session: LocalSession) -> str:
    """The access origin of the session (module docstring)."""
    access = session.model_access or "platform"
    if access == "platform":
        return "platform"
    if access == "subscription":
        cursor = await conn.execute(
            "SELECT 1 FROM runner.operator_binding WHERE session_id = %s"
            " UNION ALL SELECT 1 FROM runner.operator_origin WHERE session_id = %s",
            (session.id, session.id),
        )
        if await cursor.fetchone() is not None:
            return "operator"
    return "own"
