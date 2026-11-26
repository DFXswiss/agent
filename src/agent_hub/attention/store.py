"""Storing the attention record of a task (ADR-076 sections 1 and 4; 02-data-model.md section 2.4 `task_attention`).

`refresh(connection, task_id, now, effects)` is the only writer of `hub.task_attention`. Every writer that changes an
input of the derivation calls it at the end of its transaction (`agent_hub.sessions.taskstate.refresh_task` and
`close_task`, the writers of operation states). It

1. takes the global audit lock (`agent_hub.audit.chain.AUDIT_LOCK`, re-entrant: nearly every writer holds it already
   because `append_events` and `append_audit` take it), then locks the task's attention row (`FOR UPDATE`, inserting
   it from the task row when missing) - so two refreshes of one task serialize on the row and the later one derives
   from the earlier one's committed state (READ COMMITTED). The audit lock is taken before the attention row and held
   to the commit, so a transaction never waits for the audit lock while it holds an attention row: the attention row
   adds no cycle to the existing lock order (the order of the task, session and audit locks among the other writers is
   unchanged by this area);
2. reads the inputs (`read_inputs`) and derives the record (`agent_hub.attention.derive`);
3. writes only on a change; `updated_at` grows strictly per row (at least 1 µs past the stored value), so a client
   that keeps the newest record by `updated_at` never drops a later write as stale. `seq` counts the occasions: +1
   when the category changes, or the reason or ref inside `needs_decision` / `blocked`; `since` is the time the task
   entered its category (a phase change inside `working` changes neither). A new row starts with `seq` 1 and `since`
   = the task's last state change;
4. records, for a change, the stream frame `attention` (the `attention_row`, for the readers of the task's workgroup)
   and, when an occasion of `needs_decision` / `blocked` starts, the signal `task.attention` (`task_id`, `org_id`,
   `wg_id`, `owner_id`, `category`, `reason`, `seq`, `task_state`; sent after the commit, the notifications area
   subscribes) - not for a row created late for an older task (`since` before `now`), which describes a known state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.attention.derive import (
    BLOCKING_CODES,
    OCCASIONS,
    QUESTION_TYPES,
    QUOTA_CHOICE,
    QUOTA_WAIT,
    REFUSED_STATES,
    RUNNING_OPERATION_STATES,
    SUCCEEDED,
    Attention,
    Inputs,
    Operation,
    derive,
    waits,
)
from agent_hub.audit.chain import AUDIT_LOCK
from agent_hub.billing import credit
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.rows import as_json

Connection = psycopg.Connection[Any]
FRAME = "attention"
SIGNAL = "task.attention"
ROW_FIELDS = (
    "task_id",
    "category",
    "reason",
    "ref_kind",
    "ref_id",
    "since",
    "seq",
    "subagents_total",
    "subagents_active",
    "updated_at",
    "wait_until",
)
QUOTA_REASONS = frozenset({QUOTA_CHOICE[1], QUOTA_WAIT[1]})
"""The reasons whose rows carry `wait_until` (ADR-081 section 5); every other row leaves the field out."""
ROW_COLUMNS = ", ".join(f"a.{name}" for name in ROW_FIELDS)
_STORED = (
    "org_id, wg_id, owner_id, category, reason, ref_kind, ref_id, since, seq, subagents_total, subagents_active,"
    " wait_until"
)


@dataclass(frozen=True)
class Stored:
    """The stored record of a task (the columns `refresh` compares)."""

    org_id: UUID
    wg_id: UUID
    owner_id: UUID
    category: str
    reason: str
    ref_kind: str | None
    ref_id: UUID | None
    since: datetime
    seq: int
    subagents_total: int
    subagents_active: int
    wait_until: datetime | None = None


@dataclass(frozen=True)
class Read:
    """What `read_inputs` found: the derivation inputs, the counters and the task's last state change."""

    inputs: Inputs
    subagents_total: int
    subagents_active: int
    state_at: datetime


_INPUTS = (
    "SELECT t.state, t.state_reason, t.state_at, m.id, m.state, m.state_reason, m.wait_until, m.dfx_choice,"
    " t.model_consent, t.org_id,"
    # the end of the main session's previous turn: its newest `idle` before its newest `working` (the start of the
    # first turn when there is none); NULL while it never worked
    " CASE WHEN EXISTS (SELECT 1 FROM hub.session_transition w WHERE w.session_id = m.id AND w.state = 'working')"
    " THEN COALESCE((SELECT max(i.at) FROM hub.session_transition i WHERE i.session_id = m.id AND i.state = 'idle'"
    " AND i.seq < (SELECT max(w.seq) FROM hub.session_transition w WHERE w.session_id = m.id AND w.state = 'working')),"
    " TIMESTAMPTZ '0001-01-01 00:00:00+00') END,"
    " (SELECT max(e.received_at) FROM hub.event e WHERE e.task_id = t.id AND e.kind = 'prompt'),"
    " s.main_working, s.subagent_working, s.subagents_total, s.subagents_active,"
    " EXISTS (SELECT 1 FROM hub.operation o WHERE o.task_id = t.id AND o.state = ANY(%(running)s)),"
    # an active device of the workgroup, or one given to the whole organization (KOR-41, `org_device`)
    " (EXISTS (SELECT 1 FROM hub.wg_device d JOIN hub.wg w ON w.id = d.wg_id"
    " JOIN hub.org_member p ON p.org_id = w.org_id AND p.principal_id = d.device_id AND p.removed_seq IS NULL"
    " JOIN hub.device v ON v.id = d.device_id AND v.state = 'active'"
    " WHERE d.wg_id = t.wg_id AND d.unassigned_seq IS NULL)"
    " OR EXISTS (SELECT 1 FROM hub.org_device o"
    " JOIN hub.org_member p ON p.org_id = o.org_id AND p.principal_id = o.device_id AND p.removed_seq IS NULL"
    " JOIN hub.device v ON v.id = o.device_id AND v.state = 'active'"
    " WHERE o.org_id = t.org_id AND o.unassigned_seq IS NULL))"
    " FROM hub.task t"
    " LEFT JOIN LATERAL (SELECT id, state, state_reason, wait_until, dfx_choice FROM hub.session"
    " WHERE task_id = t.id AND depth = 0"
    " ORDER BY created_at DESC, id DESC LIMIT 1) m ON TRUE"
    " CROSS JOIN LATERAL (SELECT COALESCE(bool_or(depth = 0 AND state = 'working'), false) AS main_working,"
    " COALESCE(bool_or(depth > 0 AND state = 'working'), false) AS subagent_working,"
    " count(*) FILTER (WHERE depth > 0) AS subagents_total,"
    " count(*) FILTER (WHERE depth > 0 AND state NOT IN ('stopped', 'failed')) AS subagents_active"
    " FROM hub.session WHERE task_id = t.id) s"
    " WHERE t.id = %(task)s"
)
# Rule 3: the newest operation of the main session's last turn (created by the device after the end of its previous
# turn - its last `idle` before its newest `working`, device clock; an operation may be requested a moment before the
# guest reports `working` - and decided by the hub after the last prompt, hub clock) that matters.
_LAST_TURN = (
    "SELECT o.id, o.type, o.state, o.error_code FROM hub.operation o"
    " WHERE o.session_id = %(session)s AND o.created_at > %(turn)s"
    " AND COALESCE(o.decided_at, o.created_at) > %(prompt)s"
    " AND ((o.state = %(succeeded)s AND o.type = ANY(%(questions)s))"
    " OR (o.state = ANY(%(refused)s) AND o.error_code = ANY(%(codes)s)"
    " AND NOT EXISTS (SELECT 1 FROM hub.operation l WHERE l.task_id = o.task_id AND l.type = o.type"
    " AND l.state = %(succeeded)s AND (l.created_at, l.id) > (o.created_at, o.id))))"
    " ORDER BY o.created_at DESC, o.id DESC LIMIT 1"
)
_NO_PROMPT = datetime.fromisoformat("0001-01-01T00:00:00+00:00")


def _credit_exhausted(connection: Connection, inputs: tuple[Any, ...], now: datetime) -> bool:
    """Whether the org's credit is exhausted - read only for a main session waiting for model quota."""
    main_state, main_reason, wait_until, org_id = inputs
    if not waits(main_state, main_reason, wait_until):
        return False
    return credit.exhausted(connection, org_id, now)


def read_inputs(connection: Connection, task_id: UUID | str, now: datetime) -> Read:
    """The inputs of the derivation of a known task."""
    row = cast(
        tuple[Any, ...],
        connection.execute(_INPUTS, {"task": task_id, "running": list(RUNNING_OPERATION_STATES)}).fetchone(),
    )
    state, reason, state_at, main_id, main_state, main_reason, wait_until, dfx_choice, consent, org_id = row[:10]
    turn, prompt, main_working, subagent_working, total, active, running, device = row[10:]
    last_turn: tuple[Operation, ...] = ()
    if turn is not None:
        found = connection.execute(
            _LAST_TURN,
            {
                "session": main_id,
                "turn": turn,
                "prompt": prompt or _NO_PROMPT,
                "succeeded": SUCCEEDED,
                "questions": list(QUESTION_TYPES),
                "refused": sorted(REFUSED_STATES),
                "codes": list(BLOCKING_CODES),
            },
        ).fetchall()
        last_turn = tuple(Operation(*operation) for operation in found)
    inputs = Inputs(
        task_state=state,
        task_reason=reason,
        main_id=main_id,
        main_state=main_state,
        main_reason=main_reason,
        last_turn=last_turn,
        device_assigned=bool(device),
        operation_running=bool(running),
        main_working=bool(main_working),
        subagent_working=bool(subagent_working),
        main_wait_until=wait_until,
        main_dfx_choice=bool(dfx_choice),
        model_consent=bool(consent),
        credit_exhausted=_credit_exhausted(connection, (main_state, main_reason, wait_until, org_id), now),
    )
    return Read(inputs, int(total), int(active), state_at)


def _lock(connection: Connection, task_id: UUID | str, now: datetime) -> tuple[Stored, bool] | None:
    """The task's attention row held `FOR UPDATE` and whether this call inserted it; None for an unknown task."""
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
    inserted = connection.execute(
        "INSERT INTO hub.task_attention (task_id, org_id, wg_id, owner_id, category, reason, since, seq,"
        " subagents_total, subagents_active, updated_at)"
        " SELECT id, org_id, wg_id, owner_id, 'working', 'queued', state_at, 1, 0, 0, %s FROM hub.task WHERE id = %s"
        " ON CONFLICT (task_id) DO NOTHING RETURNING task_id",
        (now, task_id),
    ).fetchone()
    row = connection.execute(
        f"SELECT {_STORED} FROM hub.task_attention WHERE task_id = %s FOR UPDATE", (task_id,)
    ).fetchone()
    return None if row is None else (Stored(*row), inserted is not None)


def _next(current: Stored, new: bool, derived: Attention, read: Read, now: datetime) -> Stored:
    """The record to store (module docstring, step 3)."""
    if new:
        seq, since = 1, read.state_at
    elif derived.category != current.category:
        seq, since = current.seq + 1, now
    elif derived.category in OCCASIONS and (derived.reason, derived.ref_kind, derived.ref_id) != (
        current.reason,
        current.ref_kind,
        current.ref_id,
    ):
        seq, since = current.seq + 1, current.since
    else:
        seq, since = current.seq, current.since
    return Stored(
        current.org_id,
        current.wg_id,
        current.owner_id,
        derived.category,
        derived.reason,
        derived.ref_kind,
        derived.ref_id,
        since,
        seq,
        read.subagents_total,
        read.subagents_active,
        derived.wait_until,
    )


def row_json(connection: Connection, task_id: UUID | str) -> dict[str, Any] | None:
    """The `attention_row` of a task (API form), None without a record."""
    row = connection.execute(
        f"SELECT {ROW_COLUMNS} FROM hub.task_attention a WHERE a.task_id = %s", (task_id,)
    ).fetchone()
    return None if row is None else attention_json(row)


def attention_json(row: tuple[Any, ...]) -> dict[str, Any]:
    """A raw row in the order of `ROW_FIELDS` as the `attention_row` (`wait_until` only for a quota reason)."""
    found = {name: as_json(value) for name, value in zip(ROW_FIELDS, row, strict=True)}
    if found["reason"] not in QUOTA_REASONS:
        del found["wait_until"]
    return found


def refresh(connection: Connection, task_id: UUID | str, now: datetime, effects: Effects) -> bool:
    """Derive and store the record of a task in the caller's transaction (module docstring); whether it changed. A
    task without any session (captured, never started; ADR-100 section 6) has no record."""
    if connection.execute("SELECT 1 FROM hub.session WHERE task_id = %s LIMIT 1", (task_id,)).fetchone() is None:
        return False
    locked = _lock(connection, task_id, now)
    if locked is None:
        return False
    current, new = locked
    read = read_inputs(connection, task_id, now)  # known: the attention row was inserted from the task row
    stored = _next(current, new, derive(read.inputs), read, now)
    if stored == current and not new:
        return False
    connection.execute(
        "UPDATE hub.task_attention SET category = %s, reason = %s, ref_kind = %s, ref_id = %s, since = %s, seq = %s,"
        " subagents_total = %s, subagents_active = %s, wait_until = %s,"
        " updated_at = GREATEST(%s, updated_at + interval '1 microsecond') WHERE task_id = %s",
        (
            stored.category,
            stored.reason,
            stored.ref_kind,
            stored.ref_id,
            stored.since,
            stored.seq,
            stored.subagents_total,
            stored.subagents_active,
            stored.wait_until,
            now,
            task_id,
        ),
    )
    effects.org_frame(stored.org_id, FRAME, row_json(connection, task_id) or {}, wg_id=stored.wg_id)
    # A row created late for an older task (before the backfill reached it) describes a known state: no signal.
    fresh = not new or stored.since >= now
    if stored.category in OCCASIONS and fresh and (new or stored.seq != current.seq):
        effects.signal(
            SIGNAL,
            task_id=UUID(str(task_id)),
            org_id=stored.org_id,
            wg_id=stored.wg_id,
            owner_id=stored.owner_id,
            category=stored.category,
            reason=stored.reason,
            seq=stored.seq,
            task_state=read.inputs.task_state,
        )
    return True
