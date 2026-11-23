"""Event pull of a device (`GET /v1/device/pull`, 03-api.md section 6.1, 05-runner.md section 2.3).

The runner keeps one cursor per scope and pulls with `?cursor=<scope_id>:<seq>` (repeatable) `&kinds=a,b&limit=n`.
Every cursor must name a scope the device reads (`agent_hub.device.access`): the scope of an assigned workgroup
(`device:wg`) or the org scope of an org in which the device is assigned to a workgroup (`device:org`); any other scope,
known or not, is `404 not_found`. Without a cursor the pull covers every scope the device reads from the start.

What a scope returns:

- workgroup scope: every kind except `session.terminal` (terminal output is read only by browsers, ADR-016);
- org scope: `org.profile`, `wg.profile`, `member.profile`, `device.profile` and the org-level whiteboard kinds.

`kinds` narrows this further (an unknown kind is `400 request_invalid`; `session.terminal` is never returned). The
answer holds at most `limit` events (default 200, max 1000): the scopes in the order of the cursors (sorted by scope
ID without cursors), each ascending by `scope_seq`, as `{scope_id, scope_seq, received_at, event}`; `more` is true
while further matching events exist after the returned ones.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.device.access import DeviceView
from agent_hub.errors import HubError
from agent_hub.events.read import TERMINAL_KIND, read_events
from agent_hub.log.events import KIND_RULES
from agent_hub.log.state import ORG

Connection = psycopg.Connection[Any]
PAGE_DEFAULT = 200
PAGE_MAX = 1000
WG_KINDS = frozenset(rule.kind for rule in KIND_RULES if rule.scope != ORG) - {TERMINAL_KIND}
ORG_KINDS = frozenset(
    {
        "org.profile",
        "wg.profile",
        "member.profile",
        "device.profile",
        "whiteboard.entry",
        "whiteboard.edit",
        "whiteboard.delete",
    }
)
KNOWN_KINDS = frozenset(rule.kind for rule in KIND_RULES)


@dataclass(frozen=True)
class Cursor:
    """A scope and the last `scope_seq` the device has."""

    scope_id: UUID
    after: int


def scope_cursors(view: DeviceView, cursors: Sequence[Cursor]) -> list[tuple[Cursor, str]]:
    """The cursors with their scope kind; every scope must be readable (`404 not_found`)."""
    if not cursors:
        scopes = sorted((scope_id, scope) for item in view.orgs for scope, scope_id in item.scopes())
        return [(Cursor(UUID(scope_id), 0), scope) for scope_id, scope in scopes]
    found: list[tuple[Cursor, str]] = []
    for cursor in cursors:
        readable = view.scope(cursor.scope_id)
        if readable is None:
            raise HubError("not_found")
        found.append((cursor, readable[0]))
    return found


def allowed_kinds(scope: str, kinds: Collection[str] | None) -> list[str]:
    """The kinds a scope returns to a device, narrowed by the request's `kinds`."""
    base = ORG_KINDS if scope == ORG else WG_KINDS
    return sorted(base if kinds is None else base & set(kinds))


def check_kinds(kinds: Collection[str] | None) -> None:
    """Every requested kind is a kind of the event matrix (04-crypto.md section 4.3)."""
    if kinds is not None and not set(kinds) <= KNOWN_KINDS:
        raise HubError("request_invalid", detail={"pointers": ["/kinds"]})


def pull(
    connection: Connection,
    view: DeviceView,
    cursors: Sequence[Cursor],
    *,
    kinds: Collection[str] | None = None,
    limit: int = PAGE_DEFAULT,
) -> dict[str, Any]:
    """The pull answer (module docstring)."""
    check_kinds(kinds)
    events: list[dict[str, Any]] = []
    more = False
    for cursor, scope in scope_cursors(view, cursors):
        selected = allowed_kinds(scope, kinds)
        if not selected:
            continue
        remaining = limit - len(events)
        page = read_events(connection, cursor.scope_id, after=cursor.after, limit=max(remaining, 1), kinds=selected)
        if remaining == 0:
            if page.events:
                more = True
                break
            continue
        events.extend({"scope_id": str(cursor.scope_id), **item} for item in page.events)
        more = more or page.next_after is not None
    return {"events": events, "more": more}
