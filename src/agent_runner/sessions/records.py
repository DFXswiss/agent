"""Local session rows and their transitions (02-data-model.md sections 2.4.1 and 4 `session`, 03-api.md section 6.1
`session_transition`, 05-runner.md section 6).

`runner.session` holds one row per session this device was asked to run (`credential_owner` = the task owner).
Every state change goes through `advance(conn, outbox, session_id, state, ...)`, inside the caller's transaction: the
row is locked (`FOR UPDATE`), the change is checked against `allowed`, the row is updated and one outbox item
`session_transition` is written in the same transaction (05 section 2.3: local storage before the network). The
session start (step results) and the journal loop (guest `session.state` records) both use it, so a state reported
twice (for example `sandbox_ready` by the start and by the guest journal) becomes one transition, and a late report
never moves a session backwards.

Transition rules (`allowed(current, new)`):
- nothing leaves `stopped` or `failed`; a repeated state is no transition;
- the start states `requested` -> `vm_assigning` -> `vm_ready` -> `sandbox_ready` -> `agent_starting` ->
  `prompt_delivering` only move forward, and only while the session is still in that chain (a resumed session's
  guest reports `sandbox_ready`/`agent_starting` again; RUN-05 owns resumption);
- from `stopping` only `stopped` and `failed`;
- every other state (`working`, `idle`, `suspended`, `resuming`, `stopping`, `stopped`, `failed`) is accepted.

Lock order: `advance` takes the outbox's transaction advisory lock (`sync.outbox.LOCK_ID`, which every outbox writer
holds until its commit) **before** the row lock. A journal page writes outbox items first and may lock a session row
later in the same transaction, so taking the two locks in the other order here would deadlock against it.

The transition carries the device time (`at`), the reason, and - once known - `model_access`, `model_id`, the agent
version (the Claude Code version of the VM's golden image, ADR-021: `agent_kind` is always `claude-code`, the hub's
default), `claude_session_id_present` and `vm_id` (ADR-075: the VM the session runs on, from `vm_ready` on - the VM
manager names it on the row when it chose the VM, so `vm_assigning` never carries one).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from agent_runner.sync.outbox import LOCK_ID

START_CHAIN = ("requested", "vm_assigning", "vm_ready", "sandbox_ready", "agent_starting", "prompt_delivering")
FINAL = frozenset({"stopped", "failed"})
STATES = frozenset({*START_CHAIN, "working", "idle", "suspended", "resuming", "stopping", "stopped", "failed"})
FAILURE_REASONS = frozenset(
    {
        "model_access_unavailable",
        "vm_start_failed",
        "guest_unreachable",
        "sandbox_failed",
        "agent_start_failed",
        "agent_exited",
        "prompt_refused",
        "delivery_failed",
        "device_offline",
        "device_shutdown",
        "capacity_exhausted",
        "runtime_exceeded",
        "budget_exceeded",
        "credit_exhausted",
        "oom",
        "unassigned",
    }
)
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
COLUMNS = (
    "id::text AS id, task_id::text AS task_id, org_id::text AS org_id, wg_id::text AS wg_id, vm_id,"
    " parent_id::text AS parent_id, depth, state, claude_session_id, model_access, model_id,"
    " credential_owner::text AS owner_id, created_at, ready_at, ended_at"
)


class Outbox(Protocol):
    async def enqueue(self, conn: AsyncConnection[Any], item_kind: str, body: Mapping[str, object]) -> int: ...


@dataclass(frozen=True)
class LocalSession:
    id: str
    task_id: str
    org_id: str
    wg_id: str
    vm_id: str | None
    parent_id: str | None
    depth: int
    state: str
    claude_session_id: str | None
    model_access: str | None
    model_id: str | None
    owner_id: str | None
    created_at: datetime
    ready_at: datetime | None
    ended_at: datetime | None


def time_text(seconds: float) -> str:
    """`2026-11-21T12:00:00.000Z` (contracts `time`) of a Unix time."""
    value = datetime.fromtimestamp(seconds, UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def allowed(current: str, new: str) -> bool:
    """Whether `current` -> `new` is a transition (see the module docstring)."""
    if new not in STATES or current in FINAL or new == current:
        return False
    if new in START_CHAIN:
        return current in START_CHAIN and START_CHAIN.index(new) > START_CHAIN.index(current)
    if current == "stopping":
        return new in FINAL
    return True


async def insert(
    conn: AsyncConnection[Any],
    *,
    session_id: str,
    task_id: str,
    org_id: str,
    wg_id: str,
    parent_id: str | None,
    depth: int,
    owner_id: str,
    created_at: datetime,
) -> bool:
    """Record a session in state `requested`; False when it is already known (never replaced)."""
    cur = await conn.execute(
        "INSERT INTO runner.session (id, task_id, org_id, wg_id, parent_id, depth, state, credential_owner,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, 'requested', %s, %s) ON CONFLICT (id) DO NOTHING",
        (session_id, task_id, org_id, wg_id, parent_id, depth, owner_id, created_at),
    )
    return cur.rowcount == 1


async def get(conn: AsyncConnection[Any], session_id: str, *, lock: bool = False) -> LocalSession | None:
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(
        f"SELECT {COLUMNS} FROM runner.session WHERE id = %s" + (" FOR UPDATE" if lock else ""), (session_id,)
    )
    row = await cursor.fetchone()
    return None if row is None else LocalSession(**row)


async def listed(conn: AsyncConnection[Any], *, vm_id: str | None = None) -> list[LocalSession]:
    """Sessions that have not ended (only those of `vm_id` when given), oldest first."""
    query = f"SELECT {COLUMNS} FROM runner.session WHERE state <> ALL(%s)"
    params: list[object] = [sorted(FINAL)]
    if vm_id is not None:
        query += " AND vm_id = %s"
        params.append(vm_id)
    cursor = conn.cursor(row_factory=dict_row)
    await cursor.execute(query + " ORDER BY created_at, id", params)
    return [LocalSession(**row) for row in await cursor.fetchall()]


async def update(conn: AsyncConnection[Any], session_id: str, **fields: object) -> None:
    """Set columns (names are keyword names chosen by the code, never input)."""
    names = sorted(fields)
    assignments = ", ".join(f"{name} = %s" for name in names)
    await conn.execute(
        f"UPDATE runner.session SET {assignments} WHERE id = %s", [*(fields[name] for name in names), session_id]
    )


async def agent_version(conn: AsyncConnection[Any], vm_id: str | None) -> str | None:
    """The Claude Code version of the golden image the session's VM was cloned from."""
    if vm_id is None:
        return None
    cur = await conn.execute(
        "SELECT g.claude_version FROM runner.vm v JOIN runner.golden g ON g.version = v.golden_version WHERE v.id = %s",
        (vm_id,),
    )
    row = await cur.fetchone()
    version = None if row is None else str(row[0])
    return version if version is not None and _VERSION_RE.fullmatch(version) else None


def transition_body(
    session: LocalSession, state: str, reason: str | None, at: str, version: str | None
) -> dict[str, object]:
    body: dict[str, object] = {"session_id": session.id, "state": state, "reason": reason, "at": at}
    if version is not None:
        body["agent_version"] = version
    if session.model_access is not None:
        body["model_access"] = session.model_access
    if session.model_id is not None:
        body["model_id"] = session.model_id
    if session.claude_session_id is not None:
        body["claude_session_id_present"] = True
    if session.vm_id is not None:
        body["vm_id"] = session.vm_id
    return body


def check_reason(state: str, reason: str | None) -> str | None:
    """A failed session needs a reason of the closed list; other reasons are lower-case codes or None."""
    if state == "failed":
        if reason not in FAILURE_REASONS:
            raise ValueError("a failed session needs a session failure reason")
        return reason
    if reason is not None and not _REASON_RE.fullmatch(reason):
        raise ValueError("reason must be an error code")
    return reason


async def advance(
    conn: AsyncConnection[Any],
    outbox: Outbox,
    session_id: str,
    state: str,
    *,
    at: str,
    reason: str | None = None,
    now: datetime | None = None,
    fields: Mapping[str, object] | None = None,
) -> LocalSession | None:
    """Move the session to `state` and write its `session_transition`; None when that is no transition.

    `fields` are further columns set together with the state (`vm_id`, `model_access`, ...); they are written even
    when the state does not change. `now` stamps `ready_at` (first `prompt_delivering`) and `ended_at`.
    """
    reason = check_reason(state, reason)
    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
    session = await get(conn, session_id, lock=True)
    if session is None:
        return None
    changes: dict[str, object] = dict(fields or {})
    move = allowed(session.state, state)
    if move:
        changes["state"] = state
        stamp = now or datetime.now(UTC)
        if state == "prompt_delivering" and session.ready_at is None:
            changes["ready_at"] = stamp
        if state in FINAL:
            changes["ended_at"] = stamp
    if changes:
        await update(conn, session_id, **changes)
    if not move:
        return None
    updated = await get(conn, session_id)
    assert updated is not None
    version = await agent_version(conn, updated.vm_id)
    await outbox.enqueue(conn, "session_transition", transition_body(updated, state, reason, at, version))
    return updated


async def known_ids(conn: AsyncConnection[Any], ids: Sequence[str]) -> set[str]:
    cur = await conn.execute("SELECT id::text FROM runner.session WHERE id = ANY(%s::uuid[])", (list(ids),))
    return {str(row[0]) for row in await cur.fetchall()}
