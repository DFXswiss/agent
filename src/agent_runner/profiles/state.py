"""The rights profiles the hub sent, kept in the store (ADR-094 section 3; section `agent_profiles` of
`GET /v1/device/state`; runner migration `0035_agent_profiles`).

- `store(conn, desired)` (reconcile step `agent_profiles`): keeps the section of the last desired state. A
  malformed section fails the step (`wire.ResponseInvalid`); a desired state without it (an older hub, which knows no
  profiles) clears the stored one.
- `remember_child(conn, child, profile)`: the effective profile of a child the hub accepted with a profile of its
  own (`child_profile` of the registration's answer), so the child starts restricted before the next desired state
  lists it.
- `effective(conn, session)`: the effective profile of a local session: the stored section's entry, else the child's
  remembered answer, else - for a child the section does not list yet - its parent's (a child without a profile of
  its own has its parent's rights); None without a section (no profiles at this hub) or for a session of no profile.
- `named(conn, wg_id, name)`: what a profile name resolves to in a workgroup (for the pre-check of `subagent.request`);
  None for an unknown name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.profiles.model import Profile, parse
from agent_runner.reconcile import wire

SECTION = "agent_profiles"


def _check(section: object) -> dict[str, Any]:
    data = wire.obj(section, SECTION)
    api, sessions, workgroups = data.get("api"), data.get("sessions"), data.get("workgroups")
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise wire.ResponseInvalid(SECTION)
    try:
        for session_id, value in wire.obj(sessions, SECTION).items():
            wire.uuid_text(session_id, SECTION)
            parse(value)
        for wg_id, names in wire.obj(workgroups, SECTION).items():
            wire.uuid_text(wg_id, SECTION)
            for name, value in wire.obj(names, SECTION).items():
                found = parse(value)
                if found is None or found.name != name:
                    raise wire.ResponseInvalid(SECTION)
    except ValueError:
        raise wire.ResponseInvalid(SECTION) from None
    return dict(data)


async def store(conn: AsyncConnection[Any], desired: Mapping[str, Any]) -> None:
    """Keep the section of the desired state (module docstring), in the caller's transaction."""
    if SECTION not in desired:
        await conn.execute("DELETE FROM runner.profile_state")
        return
    section = _check(desired[SECTION])
    await conn.execute(
        "INSERT INTO runner.profile_state (id, section, received_at) VALUES (1, %s, now())"
        " ON CONFLICT (id) DO UPDATE SET section = EXCLUDED.section, received_at = EXCLUDED.received_at",
        (Jsonb(section),),
    )
    listed = list(section["sessions"])
    await conn.execute("DELETE FROM runner.session_profile WHERE session_id = ANY(%s::uuid[])", (listed,))


async def remember_child(conn: AsyncConnection[Any], child: str, profile: Profile | None) -> None:
    await conn.execute(
        "INSERT INTO runner.session_profile (session_id, profile, created_at) VALUES (%s, %s, now())"
        " ON CONFLICT (session_id) DO UPDATE SET profile = EXCLUDED.profile",
        (child, None if profile is None else Jsonb(profile.as_json())),
    )


async def _section(conn: AsyncConnection[Any]) -> dict[str, Any] | None:
    cursor = await conn.execute("SELECT section FROM runner.profile_state WHERE id = 1")
    row = await cursor.fetchone()
    return None if row is None else dict(row[0])


async def effective(conn: AsyncConnection[Any], session_id: str, parent_id: str | None) -> Profile | None:
    """The effective profile of a local session (module docstring)."""
    section = await _section(conn)
    if section is None:
        return None
    current, parent, seen = session_id, parent_id, set()
    while current not in seen:
        seen.add(current)
        if current in section["sessions"]:
            return parse(section["sessions"][current])
        cursor = await conn.execute("SELECT profile FROM runner.session_profile WHERE session_id = %s", (current,))
        row = await cursor.fetchone()
        if row is not None:
            return parse(row[0])
        if parent is None:
            return None
        cursor = await conn.execute("SELECT parent_id::text FROM runner.session WHERE id = %s", (parent,))
        found = await cursor.fetchone()
        current, parent = parent, None if found is None else found[0]
    return None


async def named(conn: AsyncConnection[Any], wg_id: str, name: str) -> Profile | None:
    section = await _section(conn)
    if section is None:
        return None
    return parse(section["workgroups"].get(wg_id, {}).get(name))
