"""Which Claude subscription record served a session's requests (ADR-102 section 1; 05-runner.md section 10.4).

The `usage` handler (`model.usage`) adds `subscription_id` to its item towards a hub whose device state has
`subscriptions.api >= 2` (`api`), so the hub can show who used how much of which subscription. Metadata only: the
record ID, never a token or a name.

`subscription(conn, session)`:

- a session whose access is not `subscription` -> None;
- bound to an entry of the operator's pool (`runner.operator_binding`): a shared subscription `sub-<record ID>` -> that
  record ID; a token file of the operator -> None (a file is no record);
- not bound, but once provisioned from the pool (`runner.operator_origin`: the binding ends with the session, its
  late usage must not be taken for the owner's own) -> None;
- otherwise the owner's own subscription: the ID of the cached primary `claude_subscription` record of the session's
  owner (`runner.credential_cache`, the header's `id`), None when none is cached.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.model.operator import SHARED_PREFIX
from agent_runner.sessions.records import LocalSession

API = 2
KIND = "claude_subscription"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def api(desired: Mapping[str, Any] | None) -> bool:
    """The hub announces `subscriptions.api >= 2` in its device state."""
    found = None if desired is None else desired.get("subscriptions")
    version = found.get("api") if isinstance(found, Mapping) else None
    return isinstance(version, int) and not isinstance(version, bool) and version >= API


def _record_id(value: object) -> str | None:
    return value if isinstance(value, str) and _UUID_RE.fullmatch(value) else None


async def subscription(conn: AsyncConnection[Any], session: LocalSession) -> str | None:
    """The record ID of the subscription that serves `session` (module docstring), else None."""
    if session.model_access != "subscription":
        return None
    cursor = await conn.execute("SELECT file_name FROM runner.operator_binding WHERE session_id = %s", (session.id,))
    bound = await cursor.fetchone()
    if bound is not None:
        name = str(bound[0])
        return _record_id(name[len(SHARED_PREFIX) :]) if name.startswith(SHARED_PREFIX) else None
    cursor = await conn.execute("SELECT 1 FROM runner.operator_origin WHERE session_id = %s", (session.id,))
    if await cursor.fetchone() is not None or session.owner_id is None:
        return None
    cursor = await conn.execute(
        "SELECT record FROM runner.credential_cache WHERE owner_id = %s AND kind = %s", (session.owner_id, KIND)
    )
    cached = await cursor.fetchone()
    record = cached[0] if cached is not None else None
    header = record.get("header") if isinstance(record, Mapping) else None
    return _record_id(header.get("id")) if isinstance(header, Mapping) else None
