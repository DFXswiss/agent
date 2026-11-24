"""Storing the attention record of a task (ADR-064 sections 1 and 4; 02-data-model.md section 2.4 `task_attention`).

`refresh(connection, task_id, now, effects)` is the only writer of `hub.task_attention`. Every writer that changes an
input of the derivation calls it at the end of its transaction (`agent_hub.sessions.taskstate.refresh_task` and
`close_task`, the writers of operation states). It

1. takes the global audit lock (`agent_hub.audit.chain.AUDIT_LOCK`, re-entrant: nearly every writer holds it already
   because `append_events` and `append_audit` take it), then locks the task's attention row (`FOR UPDATE`, inserting
   it from the task row when missing) - so two refreshes of one task serialize on the row and the later one derives
   from the earlier one's committed state (READ COMMITTED). Because the audit lock always comes first and is held to
   the commit, a transaction never waits for the audit lock while it holds an attention row, and every later lock of
   the same transaction (another audit row, the rows of the task it already holds) adds no cycle to the lock order of
   the tasks area;
2. reads the inputs (`read_inputs`) and derives the record (`agent_hub.attention.derive`);
3. writes only on a change. `seq` counts the occasions: +1 when the category changes, or the reason or ref inside
   `needs_decision` / `blocked`; `since` is the time the task entered its category (a phase change inside `working`
   changes neither). A new row starts with `seq` 1 and `since` = the task's last state change;
4. records, for a change, the stream frame `attention` (the `attention_row`, for the readers of the task's workgroup)
   and, when an occasion of `needs_decision` / `blocked` starts, the signal `task.attention` (`task_id`, `org_id`,
   `wg_id`, `owner_id`, `category`, `reason`, `seq`, `task_state`; sent after the commit, the notifications area
   subscribes).
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
    REFUSED_STATES,
    RUNNING_OPERATION_STATES,
    SUCCEEDED,
    Attention,
    Inputs,
    Operation,
    derive,
)
from agent_hub.audit.chain import AUDIT_LOCK
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
)
ROW_COLUMNS = ", ".join(f"a.{name}" for name in ROW_FIELDS)
_STORED = "org_id, wg_id, owner_id, category, reason, ref_kind, ref_id, since, seq, subagents_total, subagents_active"


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


@dataclass(frozen=True)
class Read:
    """What `read_inputs` found: the derivation inputs, the counters and the task's last state change."""

    inputs: Inputs
    subagents_total: int
    subagents_active: int
    state_at: datetime


_INPUTS = (
    "SELECT t.state, t.state_reason, t.state_at, m.id, m.state, m.state_reason,"
    " (SELECT max(w.at) FROM hub.session_transition w WHERE w.session_id = m.id AND w.state = 'working'),"
    " (SELECT max(e.received_at) FROM hub.event e WHERE e.task_id = t.id AND e.kind = 'prompt'),"
    " s.main_working, s.subagent_working, s.subagents_total, s.subagents_active,"
    " EXISTS (SELECT 1 FROM hub.operation o WHERE o.task_id = t.id AND o.state = ANY(%(running)s)),"
    " EXISTS (SELECT 1 FROM hub.wg_device d JOIN hub.wg w ON w.id = d.wg_id"
    " JOIN hub.org_member p ON p.org_id = w.org_id AND p.principal_id = d.device_id AND p.removed_seq IS NULL"
    " JOIN hub.device v ON v.id = d.device_id AND v.state = 'active'"
    " WHERE d.wg_id = t.wg_id AND d.unassigned_seq IS NULL)"
    " FROM hub.task t"
    " LEFT JOIN LATERAL (SELECT id, state, state_reason FROM hub.session WHERE task_id = t.id AND depth = 0"
    " ORDER BY created_at DESC, id DESC LIMIT 1) m ON TRUE"
    " CROSS JOIN LATERAL (SELECT COALESCE(bool_or(depth = 0 AND state = 'working'), false) AS main_working,"
    " COALESCE(bool_or(depth > 0 AND state = 'working'), false) AS subagent_working,"
    " count(*) FILTER (WHERE depth > 0) AS subagents_total,"
    " count(*) FILTER (WHERE depth > 0 AND state NOT IN ('stopped', 'failed')) AS subagents_active"
    " FROM hub.session WHERE task_id = t.id) s"
    " WHERE t.id = %(task)s"
)
# Rule 3: the newest operation of the main session's last turn (created by the device at or after the start of its
# last `working`, device clock; decided by the hub after the last prompt, hub clock) that matters.
_LAST_TURN = (
    "SELECT o.id, o.type, o.state, o.error_code FROM hub.operation o"
    " WHERE o.session_id = %(session)s AND o.created_at >= %(turn)s"
    " AND COALESCE(o.decided_at, o.created_at) > %(prompt)s"
    " AND ((o.state = %(succeeded)s AND o.type = ANY(%(questions)s))"
    " OR (o.state = ANY(%(refused)s) AND o.error_code = ANY(%(codes)s)"
    " AND NOT EXISTS (SELECT 1 FROM hub.operation l WHERE l.task_id = o.task_id AND l.type = o.type"
    " AND l.state = %(succeeded)s AND (l.created_at, l.id) > (o.created_at, o.id))))"
    " ORDER BY o.created_at DESC, o.id DESC LIMIT 1"
)
_NO_PROMPT = datetime.fromisoformat("0001-01-01T00:00:00+00:00")


def read_inputs(connection: Connection, task_id: UUID | str) -> Read:
    """The inputs of the derivation of a known task."""
    row = cast(
        tuple[Any, ...],
        connection.execute(_INPUTS, {"task": task_id, "running": list(RUNNING_OPERATION_STATES)}).fetchone(),
    )
    state, reason, state_at, main_id, main_state, main_reason, turn, prompt = row[:8]
    main_working, subagent_working, total, active, running, device = row[8:]
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
    )


def row_json(connection: Connection, task_id: UUID | str) -> dict[str, Any] | None:
    """The `attention_row` of a task (API form), None without a record."""
    row = connection.execute(
        f"SELECT {ROW_COLUMNS} FROM hub.task_attention a WHERE a.task_id = %s", (task_id,)
    ).fetchone()
    return None if row is None else attention_json(row)


def attention_json(row: tuple[Any, ...]) -> dict[str, Any]:
    """A raw row in the order of `ROW_FIELDS` as the `attention_row`."""
    return {name: as_json(value) for name, value in zip(ROW_FIELDS, row, strict=True)}


def refresh(connection: Connection, task_id: UUID | str, now: datetime, effects: Effects) -> bool:
    """Derive and store the record of a task in the caller's transaction (module docstring); whether it changed."""
    locked = _lock(connection, task_id, now)
    if locked is None:
        return False
    current, new = locked
    read = read_inputs(connection, task_id)  # known: the attention row was inserted from the task row
    stored = _next(current, new, derive(read.inputs), read, now)
    if stored == current and not new:
        return False
    connection.execute(
        "UPDATE hub.task_attention SET category = %s, reason = %s, ref_kind = %s, ref_id = %s, since = %s, seq = %s,"
        " subagents_total = %s, subagents_active = %s, updated_at = %s WHERE task_id = %s",
        (
            stored.category,
            stored.reason,
            stored.ref_kind,
            stored.ref_id,
            stored.since,
            stored.seq,
            stored.subagents_total,
            stored.subagents_active,
            now,
            task_id,
        ),
    )
    effects.org_frame(stored.org_id, FRAME, row_json(connection, task_id) or {}, wg_id=stored.wg_id)
    if stored.category in OCCASIONS and (new or stored.seq != current.seq):
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
