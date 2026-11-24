"""Whose personal connection an operation uses (KONZEPT A15 and E-19, ADR-068; 05-runner.md section 12 step 3).

A personal connection must never be used on behalf of another person. Several people may write in one task (shared
sessions), so an operation never uses the task owner's connection as such: it uses the connection of the person
whose instruction started the session's current turn - the **acting user** - and nobody else's. This module is the
only place that decides it and the only place that opens a personal credential for an operation (a structural test
keeps it so).

- `PERSONAL_KINDS`: the credential kinds that are one person's own connection and that operations of a session use:
  `github` (every `github.*` type, `needs`). The other kinds are no connection a session uses on behalf of someone:
  `mailbox` and the login kinds are opened only for the verified user of that user's own device request
  (`login.requests`), the model access is the session's model access chosen at its start, whose consumption the
  organisation carries (A15; the model gateways and quota reads of `model`/`quota`).
- **Turn** (`prompt_accepted`, `turn_ended`; the observer `credentials/session_state.py`, which runs in the journal
  transaction in the guest's order): the verified author of an accepted prompt (`runner.prompt_event.author_id`, set
  by the inbox from the signed event; only users author prompts, a user's own agent signs as that user) becomes the
  acting user of the main session when no turn is open; a second prompt in the same open turn (no `idle` since) by
  another or an unknown author makes it unknown (NULL) until the next turn. `idle` closes the turn; the last acting
  user stays for what the turn still journals.
- `stamp(conn, session)`: the acting user at the receipt of an operation request (journal handler): the turn's
  acting user of a main session; for a child session (`depth` > 0) the acting user of the `subagent.request` that
  started it - children inherit the person who instructed their parent's turn. The stamp is stored with the operation
  and never changes: a held operation keeps it, whoever writes later.
- `cached_version(conn, operation, kind)`: the version of the acting user's cached credential (None without an acting
  user or record); `opened(context, operation, kind)`: its decrypted body for one use - `CredentialUnavailable
  ("no_actor")` without an acting user, so nothing is ever opened for an operation whose acting user is unknown.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from psycopg import AsyncConnection

from agent_runner.credentials import cache

PERSONAL_KINDS = frozenset({"github"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NO_ACTOR = (
    "This instruction cannot be attributed to exactly one person, so no personal GitHub connection is used. Ask the"
    " person who wants the GitHub action to write the request again."
)


class Acting(Protocol):
    """What the decision needs of an operation (`operations.records.Operation`)."""

    @property
    def type(self) -> str: ...

    @property
    def actor_id(self) -> str | None: ...


class Session(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def depth(self) -> int: ...


def needs(op_type: str) -> str | None:
    """The personal credential kind an operation type uses (None: none)."""
    return "github" if op_type.startswith("github.") else None


async def prompt_accepted(conn: AsyncConnection[Any], session_id: str, prompt_id: str) -> None:
    """A prompt of the session was accepted: its verified author acts in the turn (module docstring, "Turn"). A prompt
    ID that names no stored prompt delivered to this session has no known author."""
    known = prompt_id if _UUID_RE.fullmatch(prompt_id) else None
    await conn.execute(
        "UPDATE runner.session s SET turn_actor = CASE WHEN s.turn_open AND s.turn_actor IS DISTINCT FROM a.author"
        " THEN NULL ELSE a.author END, turn_open = true FROM (SELECT (SELECT e.author_id FROM runner.prompt_event e"
        " JOIN runner.prompt_delivery d ON d.event_id = e.event_id WHERE e.event_id = %s AND d.session_id = %s)"
        " AS author) a WHERE s.id = %s",
        (known, session_id, session_id),
    )


async def turn_ended(conn: AsyncConnection[Any], session_id: str) -> None:
    await conn.execute("UPDATE runner.session SET turn_open = false WHERE id = %s", (session_id,))


async def stamp(conn: AsyncConnection[Any], session: Session) -> str | None:
    """The acting user of an operation the session requests now (module docstring)."""
    if session.depth > 0:
        cur = await conn.execute(
            "SELECT actor_id::text FROM runner.operation WHERE child_session_id = %s AND type = 'subagent.request'"
            " AND actor_id IS NOT NULL LIMIT 1",
            (session.id,),
        )
    else:
        cur = await conn.execute("SELECT turn_actor::text FROM runner.session WHERE id = %s", (session.id,))
    row = await cur.fetchone()
    return None if row is None else row[0]


async def cached_version(conn: AsyncConnection[Any], operation: Acting, kind: str) -> int | None:
    """The version of the acting user's cached credential of `kind` (None without an acting user or record)."""
    if operation.actor_id is None:
        return None
    found = await cache.cached(conn, operation.actor_id, kind)
    return None if found is None else found[0]


@asynccontextmanager
async def opened(context: Any, operation: Acting, kind: str) -> AsyncIterator[dict[str, Any]]:
    """The acting user's credential body of `kind` for one use (`cache.opened`); never anybody else's."""
    if kind not in PERSONAL_KINDS or needs(operation.type) != kind:
        raise ValueError("not a personal connection of this operation")
    if operation.actor_id is None:
        raise cache.CredentialUnavailable("no_actor")
    async with cache.opened(context, operation.actor_id, kind) as body:
        yield body
