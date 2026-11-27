"""Whose personal connection an operation uses (KONZEPT A15 and E-19, ADR-070; 05-runner.md section 12 step 3).

A personal connection must never be used on behalf of another person. Several people may write in one task (shared
sessions), so an operation never uses the task owner's connection as such: it uses the connection of the person
whose instruction started the session's current turn - the **acting user** - and nobody else's. This module is the
only place that decides it and the only place that opens a personal credential for an operation (a structural test
keeps it so).

- `PERSONAL_KINDS`: the credential kinds that are one person's own connection and that operations of a session use:
  `github` (every `github.*` type, `needs`). The other kinds are no connection a session uses on behalf of someone:
  `mailbox` and the login kinds are opened for the verified user of that user's own device request
  (`login.requests`); a `mailbox` record its owner released to a workgroup is opened by `mail.find` for the sessions
  of that workgroup, because its owner switched that on (`agent_runner.mail`, ADR-099 section 3); the model access
  is the session's model access chosen at its start, whose consumption the
  organisation carries (A15; the model gateways and quota reads of `model`/`quota`).
- **Turn** (`prompt_accepted`, `turn_ended`; the observer `credentials/session_state.py`, which runs in the journal
  transaction in the guest's order): the verified author of an accepted prompt (`runner.prompt_event.author_id`, set
  by the inbox from the signed event; only users and their agents author prompts, and an agent's prompt counts as its
  user's, ADR-082) becomes the acting user of the main session when no turn is open; a second prompt in the same
  open turn (no `idle` since) by another or an unknown author makes it unknown (NULL) until the next turn. `idle`
  closes the turn; the last acting user stays for what the turn still journals.
- **Platform continuation** (FIX-02): a prompt the platform hands a main session by itself - the continuation after
  credit (`billing.pause`), after a model pause (`model.fallback`) or after a model rate limit (`model.retry`) - is
  no person's prompt, but it continues the turn a person started. `continuation(conn, session_id, prompt_id)` records
  it before it is registered, with the session's `turn_actor` at that moment (the person of the interrupted turn); when
  the guest accepts it, that person acts in the new turn under the rule above (a person's prompt in the same open turn
  still makes it unknown). Without this the continued turn had no acting user, and a GitHub operation the person had
  asked for was refused with `NO_ACTOR`.
- **Turn of a child session** (`instruction_accepted`, ADR-088 section 1): a child's prompts are the instructions of
  its parent (`runner.subagent_instruction`, prompt ID = operation ID), one per turn. When the child accepts one, the
  acting user of that instruction's operation (`subagent.request` or `subagent.instruct`, the person who instructed
  the parent's turn) becomes the acting user of the child's turn; a prompt ID that names no delivered instruction of
  this child makes it unknown. An instruction still waiting for its turn changes nothing, and neither does a
  repeated acceptance of an instruction older than one the child already accepted.
- `stamp(conn, session)`: the acting user at the receipt of an operation request (journal handler) and of a forwarded
  read (ADR-089): the turn's acting user of a main session; of a child session (`depth` > 0) the acting user of its
  turn once it accepted an instruction, before that (and for a child older than the instructions) the acting user of
  the `subagent.request` that started it - children inherit the person who instructed their parent's turn. The stamp
  is stored with the operation and never changes: a held operation keeps it, whoever writes later.
- `cached_version(conn, operation, kind)`: the version of the acting user's cached credential (None without an acting
  user or record); `opened(context, operation, kind)`: its decrypted body for one use - `CredentialUnavailable
  ("no_actor")` without an acting user, so nothing is ever opened for an operation whose acting user is unknown, and
  `CredentialUnavailable("shared")` for a copy of the operator's GitHub token held by anybody but the operator's user
  (`github.operator.foreign_copy`; stored while several users could be approved).

GitHub identities (KOR-47, ADR-078): the acting user may hold several own `github` records beside the primary one,
and a task may act with a company record of its organization (`github.identity` decides which one, with the acting
user as the human of ADR-078 section 2). Every opening of a GitHub identity record for an operation goes through here
as well:

- `opened_identity(context, operation, record)`: the body of an identity record (`credentials.identities`) for one
  use. A personal record (`owner_kind` `user`) opens only when its owner is the acting user (and is no foreign copy of
  the operator's token); a company record (`org`) is no personal connection - whether the acting user may use it is
  decided by `github.identity.usable` from the verified membership log - and opens for any operation with an acting
  user. `CredentialUnavailable` (`no_actor`, `not_actor`, `shared` or the opening's own reason) otherwise.
- `primary_record_id(conn, operation)`: the record ID of the acting user's cached primary record (None without one).
- `attribution(context, user)`: `(GitHub account ID, login)` of a person's own primary `github` record, for naming
  that person as a co-author of a commit (`Co-authored-by`). This is **attribution, not use of the connection**: only
  the account ID and login leave this function, never the token; nothing is sent with it. None without a record, for
  a body without a valid account ID and login, and for a copy of the operator's token (`source:"operator"`, or a
  foreign copy).
- `instructors(conn, task_id)`: the verified authors of the task's delivered prompts that are still stored on this
  device (`prompt_event.author_id`, deliveries `typed` or `accepted`), newest prompt first, each once - the humans
  that may be named as co-authors (the task owner through the prompt that created the task). Prompt events are pruned
  a day after their delivery except the earliest accepted one of each task (`prompts.retention`), so a person whose
  prompts are all pruned can no longer be named.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from psycopg import AsyncConnection

from agent_runner.credentials import cache, identities
from agent_runner.credentials.identities import Identity
from agent_runner.github import operator
from agent_runner.sync.http import HubRefusal

PERSONAL_KINDS = frozenset({"github"})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
MAX_ACCOUNT_ID = 9_007_199_254_740_991
DELIVERED = ("typed", "accepted")
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
    ID that names no stored prompt being delivered to this session (its delivery row `registered` or `typed`; this
    observer runs before the delivery's, which marks it `accepted`) has no known author - a replayed or forged
    acceptance of an earlier prompt never brings back its author. A platform continuation of this session
    (`continuation`) acts for the person recorded with it, once: its row is removed on acceptance."""
    known = prompt_id if _UUID_RE.fullmatch(prompt_id) else None
    cursor = await conn.execute(
        "DELETE FROM runner.platform_prompt WHERE prompt_id = %s AND session_id = %s RETURNING actor_id::text",
        (known, session_id),
    )
    continued = await cursor.fetchone()
    if continued is not None:
        await conn.execute(
            "UPDATE runner.session SET turn_actor = CASE WHEN turn_open AND turn_actor IS DISTINCT FROM %s::uuid"
            " THEN NULL ELSE %s::uuid END, turn_open = true WHERE id = %s",
            (continued[0], continued[0], session_id),
        )
        return
    await conn.execute(
        "UPDATE runner.session s SET turn_actor = CASE WHEN s.turn_open AND s.turn_actor IS DISTINCT FROM a.author"
        " THEN NULL ELSE a.author END, turn_open = true FROM (SELECT (SELECT e.author_id FROM runner.prompt_event e"
        " JOIN runner.prompt_delivery d ON d.event_id = e.event_id WHERE e.event_id = %s AND d.session_id = %s"
        " AND d.state IN ('registered', 'typed'))"
        " AS author) a WHERE s.id = %s",
        (known, session_id, session_id),
    )


async def unknown_cause(conn: AsyncConnection[Any], session_id: str) -> str:
    """Why a session's operation had no acting user, for the log (FIX-02): `turn_open=<bool> last_prompt=<author|
    no_author|none>` - whether the turn was still running and whether the session's latest accepted prompt of a person
    had a verified author. A turn that is running while that prompt had an author was started by something else than
    that prompt (a second prompt in the same turn, or a turn without a prompt of a person)."""
    cursor = await conn.execute(
        "SELECT s.turn_open, (SELECT e.author_id IS NOT NULL FROM runner.prompt_delivery d JOIN runner.prompt_event e"
        " ON e.event_id = d.event_id WHERE d.session_id = s.id AND d.state = 'accepted'"
        " ORDER BY d.updated_at DESC, d.scope_seq DESC LIMIT 1) FROM runner.session s WHERE s.id = %s",
        (session_id,),
    )
    found = await cursor.fetchone()
    if found is None:
        return "turn_open=unknown last_prompt=none"
    last = "none" if found[1] is None else "author" if found[1] else "no_author"
    return f"turn_open={'true' if found[0] else 'false'} last_prompt={last}"


async def continuation(conn: AsyncConnection[Any], session_id: str, prompt_id: str) -> str | None:
    """Record a prompt the platform hands the session by itself (credit back, model pause ended, model rate limit;
    FIX-02) with the acting user of the turn it continues - the session's `turn_actor`, which stays after `idle` - and
    return that person (None: the continued turn had none). A repeated hand-over of the same prompt keeps the first
    record. Call it before the prompt is registered at the guest."""
    await conn.execute(
        "INSERT INTO runner.platform_prompt (prompt_id, session_id, actor_id)"
        " SELECT %s, s.id, s.turn_actor FROM runner.session s WHERE s.id = %s ON CONFLICT (prompt_id) DO NOTHING",
        (prompt_id, session_id),
    )
    cursor = await conn.execute(
        "SELECT actor_id::text FROM runner.platform_prompt WHERE prompt_id = %s AND session_id = %s",
        (prompt_id, session_id),
    )
    found = await cursor.fetchone()
    return None if found is None else found[0]


async def instruction_accepted(conn: AsyncConnection[Any], session_id: str, prompt_id: str) -> None:
    """A child session accepted the prompt of one of its instructions (module docstring, "Turn of a child session")."""
    known = prompt_id if _UUID_RE.fullmatch(prompt_id) else None
    cursor = await conn.execute(
        "SELECT i.actor_id::text, EXISTS (SELECT 1 FROM runner.subagent_instruction j WHERE j.child_session_id ="
        " i.child_session_id AND j.accepted_at IS NOT NULL AND (j.created_at, j.operation_id) > (i.created_at,"
        " i.operation_id)) FROM runner.subagent_instruction i WHERE i.operation_id = %s AND i.child_session_id = %s"
        " AND i.delivered_at IS NOT NULL",
        (known, session_id),
    )
    found = await cursor.fetchone()
    if found is not None and found[1]:
        return  # a replayed acceptance of an earlier instruction never brings back that person's rights
    actor = None if found is None else found[0]
    await conn.execute("UPDATE runner.session SET turn_actor = %s, turn_open = true WHERE id = %s", (actor, session_id))


async def turn_ended(conn: AsyncConnection[Any], session_id: str) -> None:
    await conn.execute("UPDATE runner.session SET turn_open = false WHERE id = %s", (session_id,))


async def stamp(conn: AsyncConnection[Any], session: Session) -> str | None:
    """The acting user of an operation the session requests now (module docstring)."""
    if session.depth > 0:
        cur = await conn.execute(
            "SELECT CASE WHEN s.turn_open OR EXISTS (SELECT 1 FROM runner.subagent_instruction i"
            " WHERE i.child_session_id = s.id AND i.accepted_at IS NOT NULL) THEN s.turn_actor::text"
            " ELSE (SELECT o.actor_id::text FROM runner.operation o WHERE o.child_session_id = s.id"
            " AND o.type = 'subagent.request' AND o.actor_id IS NOT NULL ORDER BY o.created_at, o.id LIMIT 1) END"
            " FROM runner.session s WHERE s.id = %s",
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
        if operator.foreign_copy(context, operation.actor_id, body.get("token")):
            raise cache.CredentialUnavailable("shared")
        yield body


@asynccontextmanager
async def opened_identity(context: Any, operation: Acting, record: Identity) -> AsyncIterator[dict[str, Any]]:
    """The body of a GitHub identity record for one use (module docstring)."""
    if needs(operation.type) != "github":
        raise ValueError("not a personal connection of this operation")
    if operation.actor_id is None:
        raise cache.CredentialUnavailable("no_actor")
    if record.owner_kind == identities.USER and record.owner != operation.actor_id:
        raise cache.CredentialUnavailable("not_actor")
    async with identities.opened(context, record) as body:
        if record.owner_kind == identities.USER and operator.foreign_copy(context, record.owner, body.get("token")):
            raise cache.CredentialUnavailable("shared")
        yield body


async def primary_record_id(conn: AsyncConnection[Any], operation: Acting) -> str | None:
    """The record ID of the acting user's cached primary `github` record."""
    if operation.actor_id is None:
        return None
    return await identities.primary_id(conn, operation.actor_id)


def _account(body: dict[str, Any]) -> tuple[int, str] | None:
    login, account = body.get("login"), body.get("user_id")
    if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
        return None
    if not isinstance(account, int) or isinstance(account, bool) or not 1 <= account <= MAX_ACCOUNT_ID:
        return None
    return account, login


async def attribution(context: Any, user: str) -> tuple[int, str] | None:
    """`(account ID, login)` of the person's own primary GitHub record, to name the person (module docstring)."""
    try:
        async with cache.opened(context, user, "github") as body:
            found = _account(body)
            copy = body.get("source") == "operator" or operator.foreign_copy(context, user, body.get("token"))
    except (cache.CredentialUnavailable, HubRefusal):
        return None
    return None if copy else found


async def instructors(conn: AsyncConnection[Any], task_id: str) -> list[str]:
    """The verified authors of the task's delivered prompts stored on this device, newest first, each once."""
    cur = await conn.execute(
        "SELECT e.author_id::text FROM runner.prompt_event e JOIN runner.prompt_delivery d ON d.event_id = e.event_id"
        " WHERE e.task_id = %s AND d.state = ANY(%s) AND e.author_id IS NOT NULL ORDER BY e.scope_seq DESC",
        (task_id, list(DELIVERED)),
    )
    found: list[str] = []
    for (author,) in await cur.fetchall():
        if author not in found:
            found.append(author)
    return found
