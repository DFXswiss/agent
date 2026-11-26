"""Rebuild of the support roster projection (ADR-027 section 4: `REBUILDERS`; ADR-096).

`rebuild(connection)` replays the stored platform log (verified when it was appended, so no key is needed) with the
roster rule of `agent_hub.log.platform` - a `role_grant support` binds the person to its certification valid at that
entry; the person is active while the role stands and that certification is its valid one - and sets
`support_person.active` accordingly (rows of persons no longer active stay, with their names). The release rows are
part of the membership-log projections (`agent_hub.orgs.projection`) and rebuilt with them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import psycopg

SUPPORT = "support"


def roster_of(bodies: Sequence[Mapping[str, object]]) -> set[str]:
    """The active support persons after the platform-log bodies, in order."""
    certified: dict[str, tuple[int, bool]] = {}
    bound: dict[str, int] = {}
    roles: set[str] = set()
    for seq, body in enumerate(bodies):
        kind, user = body["type"], str(body.get("user", body.get("subject", "")))
        if kind == "user_certify":
            certified[user] = (seq, True)
        elif kind == "user_decertify":
            certified[user] = (certified[user][0], False)
        elif kind == "role_grant" and body["role"] == SUPPORT:
            roles.add(user)
            bound[user] = certified[user][0]
        elif kind == "role_revoke" and body["role"] == SUPPORT:
            roles.discard(user)
    return {user for user in roles if certified[user] == (bound[user], True)}


def rebuild(connection: psycopg.Connection[Any]) -> int:
    """Replay the platform log into `support_person.active`; the number of entries replayed."""
    rows = connection.execute("SELECT entry_text, received_at FROM hub.platform_entry ORDER BY seq").fetchall()
    bodies = [cast(dict[str, object], json.loads(text)["body"]) for text, _at in rows]
    active = sorted(roster_of(bodies))
    connection.execute("UPDATE hub.support_person SET active = false WHERE NOT (user_id = ANY(%s::uuid[]))", (active,))
    for user in active:
        connection.execute(
            "INSERT INTO hub.support_person (user_id, active, changed_at) VALUES (%s, true, %s)"
            " ON CONFLICT (user_id) DO UPDATE SET active = true",
            (user, rows[-1][1]),
        )
    return len(rows)


REBUILDERS: Mapping[str, Callable[[psycopg.Connection[Any]], int]] = {"support": rebuild}
