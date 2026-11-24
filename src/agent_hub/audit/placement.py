"""Where an audit row of a request lies (ADR-063 sections 1 and 3): the IDs a request names and their placement.

* `named_ids`: the organization, workgroup, task and session a request names - path parameters of those names, else
  single query parameters; UUIDv7 text only.
* `target`: the last path parameter as `(target_kind, target_id)`.
* `place`: a **successful** row (`ok`: the actor holds the right, so the resource is one of an organization it may read
  or write) gets its missing organization, workgroup, task and session from the hub's row of the most specific ID it
  names (session, else task, else workgroup, else the operation or the event scope of the target: a workgroup or an
  organization); it is left as it is when the hub does not know the ID or when an ID the request named contradicts the
  hub's row. A **refused** row keeps the IDs it named only when they lie in an organization the actor is an active
  member of (looked up the same way); otherwise its organization, workgroup, task and session are cleared, so a
  stranger cannot place rows in another organization's audit view (the chain still holds them for the operator).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AuditRow

ID_PARAMETERS = ("org", "wg", "task", "session")
UUID_VERSION = 7
_TARGET_KIND_RE = re.compile(r"[a-z_]{1,32}")
_TARGET_ID_RE = re.compile(r"[ -~]{1,128}")


def uuid7(value: object) -> UUID | None:
    """`value` as a UUIDv7 when it is its canonical lower-case text, else None."""
    try:
        parsed = UUID(str(value))
    except ValueError:
        return None
    return parsed if parsed.version == UUID_VERSION and str(parsed) == str(value) else None


def named_ids(path_params: Mapping[str, Any], query: Mapping[str, str]) -> dict[str, UUID | None]:
    """The organization, workgroup, task and session a request names: path parameters of those names, else single
    query parameters (UUIDv7 text only)."""
    return {name: uuid7(path_params[name]) if name in path_params else uuid7(query.get(name)) for name in ID_PARAMETERS}


def target(path_params: Mapping[str, Any], id_kind: str) -> tuple[str | None, str | None]:
    """The last path parameter as `(target_kind, target_id)`: kind = its name (`id_kind` for `{id}`)."""
    if not path_params:
        return None, None
    name, value = list(path_params.items())[-1]
    kind = (id_kind if name == "id" else name).replace(".", "_")[:32]
    text = str(value)
    if _TARGET_KIND_RE.fullmatch(kind) and _TARGET_ID_RE.fullmatch(text):
        return kind, text
    return None, None


_PLACEMENT = {
    "session": "SELECT org_id, wg_id, task_id, id FROM hub.session WHERE id = %s",
    "operation": "SELECT org_id, wg_id, task_id, session_id FROM hub.operation WHERE id = %s",
    "task": "SELECT org_id, wg_id, id, NULL::uuid FROM hub.task WHERE id = %s",
    "wg": "SELECT org_id, id, NULL::uuid, NULL::uuid FROM hub.wg WHERE id = %s",
    "org": "SELECT id, NULL::uuid, NULL::uuid, NULL::uuid FROM hub.org WHERE id = %s",
}
# Targets of `{id}` routes and event scopes that name the read resource itself.
_TARGET_ANCHORS = {"session": ("session",), "operation": ("operation",), "scope": ("wg", "org")}


def _anchor(row: AuditRow) -> tuple[tuple[str, ...], UUID] | None:
    """The tables and the most specific ID of the row to place it by (module docstring)."""
    for kind, value in (("session", row.session_id), ("task", row.task_id), ("wg", row.wg_id)):
        if value is not None:
            return (kind,), value
    tables = _TARGET_ANCHORS.get(row.target_kind or "")
    target = uuid7(row.target_id)
    return None if tables is None or target is None else (tables, target)


def _found(connection: psycopg.Connection[Any], row: AuditRow) -> tuple[Any, ...] | None:
    anchor = _anchor(row)
    if anchor is None:
        return None
    tables, value = anchor
    for table in tables:
        found = connection.execute(_PLACEMENT[table], (value,)).fetchone()
        if found is not None:
            return tuple(found)
    return None


def _contradicts(row: AuditRow, found: tuple[Any, ...]) -> bool:
    named = (row.org_id, row.wg_id, row.task_id, row.session_id)
    pairs = zip(named, found, strict=True)
    return any(mine is not None and theirs is not None and mine != theirs for mine, theirs in pairs)


def _confined(connection: psycopg.Connection[Any], row: AuditRow) -> AuditRow:
    if row.org_id is None and row.wg_id is None and row.task_id is None and row.session_id is None:
        return row
    named_below_org = row.wg_id is not None or row.task_id is not None or row.session_id is not None
    found = _found(connection, row) if named_below_org else (row.org_id, None, None, None)
    org = None if found is None or _contradicts(row, found) else found[0]
    member = (
        org is not None
        and connection.execute(
            "SELECT 1 FROM hub.org_member WHERE org_id = %s AND principal_id = %s AND removed_seq IS NULL",
            (org, row.actor_id),
        ).fetchone()
    )
    return row if member else replace(row, org_id=None, wg_id=None, task_id=None, session_id=None)


def place(connection: psycopg.Connection[Any], row: AuditRow) -> AuditRow:
    """The row placed as the module docstring describes."""
    if row.result != "ok":
        return _confined(connection, row)
    found = _found(connection, row)
    if found is None or _contradicts(row, found):
        return row
    named = (row.org_id, row.wg_id, row.task_id, row.session_id)
    org, wg, task, session = (mine or theirs for mine, theirs in zip(named, found, strict=True))
    return replace(row, org_id=org, wg_id=wg, task_id=task, session_id=session)
