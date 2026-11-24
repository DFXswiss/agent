"""Operation rows of the runner store (02-data-model.md section 4 `operation`, migration 0004; 05-runner.md section 12).

One row per `operation.request` the guest journaled, keyed by the guest's `operation_id`, with the plaintext
parameters (local only). States are those of the hub (`contracts/common.json#/$defs/operation_state`):

| State | Meaning on this device |
|---|---|
| `received` | journaled; formal checks, dedupe tag and registration at the hub are running |
| `rejected`, `denied` | the hub's decision (local rejections are registered as `rejected`); final |
| `duplicate` | the hub found an original holding the slot; final once the session got its answer |
| `accepted` | registered; the executor has not started |
| `executing` | the executor runs (`operation_transition executing` written) |
| `succeeded`, `failed` | the executor's outcome (`operation.result` event and transition written); final |
| `expired` | 10 min in `received` without a decision of the hub (`hub_unreachable`); final |

`answer` is the outcome given (or to be given) to the session's pending MCP call; `answered_at` is set once the guest
took it.

A **held** operation (KOR-25, migration 0015, 05-runner.md section 12 step 3) stays `received` with `held_reason`
(`github_connection`, or `github_access` with its next step `held_next`: migration 0019, KOR-65), `held_repo` (the
checked repository of its `session.status` `waiting` event, if any),
`held_until` (the request plus 15 min), `held_notified_at` (when its `waiting` event was written), `held_ended_at`
(when the hold ended: the acting user connected, the session ended, or `held_until` passed) and `held_credential`
(the GitHub generation of the device the last check refused, `credentials.identities.generation`, KOR-47). `actor_id`
is the acting user stamped at the receipt (migration 0016, HOT-14). It is held while
`held_until` is set and `held_ended_at` is not (`held`). `held_count` and `last_waiting` serve the flood limits per
session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

STATES = frozenset(
    {"received", "rejected", "accepted", "duplicate", "denied", "executing", "succeeded", "failed", "expired"}
)
FINAL = frozenset({"rejected", "denied", "duplicate", "succeeded", "failed", "expired"})
OPEN = ("received", "accepted", "executing")
JSON_COLUMNS = frozenset({"params", "result", "local_error", "checked", "registration", "answer", "held_next"})
COLUMNS = frozenset(
    {
        "state",
        "result",
        "error_code",
        "dedupe_tag",
        "attempt",
        "local_error",
        "checked",
        "registration",
        "duplicate_of",
        "child_session_id",
        "result_event_id",
        "retryable",
        "answer",
        "answered_at",
        "held_reason",
        "held_repo",
        "held_until",
        "held_notified_at",
        "held_ended_at",
        "held_credential",
        "held_next",
    }
)
_SELECT = (
    "SELECT id::text AS id, session_id::text AS session_id, task_id::text AS task_id, wg_id::text AS wg_id,"
    " org_id::text AS org_id, vm_id, pending_id::text AS pending_id, type, state, params, result, error_code,"
    " dedupe_tag, attempt, requested_at, idempotency_key, local_error, checked, registration,"
    " duplicate_of::text AS duplicate_of, child_session_id::text AS child_session_id,"
    " result_event_id::text AS result_event_id, retryable, answer, answered_at, created_at,"
    " held_reason, held_repo, held_until, held_notified_at, held_ended_at, held_credential, held_next,"
    " actor_id::text AS actor_id, EXTRACT(EPOCH FROM (now() - created_at))::float8 AS age FROM runner.operation"
)


@dataclass(frozen=True)
class Operation:
    id: str
    session_id: str
    task_id: str
    wg_id: str
    org_id: str
    vm_id: str
    pending_id: str
    type: str
    state: str
    params: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    dedupe_tag: str | None
    attempt: int
    requested_at: str
    idempotency_key: str | None
    local_error: dict[str, Any] | None
    checked: dict[str, Any] | None
    registration: dict[str, Any] | None
    duplicate_of: str | None
    child_session_id: str | None
    result_event_id: str | None
    retryable: bool | None
    answer: dict[str, Any] | None
    answered_at: datetime | None
    created_at: datetime
    held_reason: str | None
    held_repo: str | None
    held_until: datetime | None
    held_notified_at: datetime | None
    held_ended_at: datetime | None
    held_credential: int | None
    held_next: dict[str, Any] | None
    """The next step of an operation held for repository access (`github_access`, migration 0019, KOR-65)."""
    actor_id: str | None
    """The acting user stamped at the receipt (`credentials.acting`): whose personal connection the operation may use;
    None: nobody's."""
    age: float
    """Seconds since the row was written (store clock)."""

    @property
    def held(self) -> bool:
        """The operation is held for its acting user's GitHub connection (KOR-25, HOT-14)."""
        return self.held_until is not None and self.held_ended_at is None


def _row(data: Mapping[str, Any]) -> Operation:
    return Operation(**dict(data))


async def insert(
    conn: AsyncConnection[Any],
    *,
    operation_id: str,
    session_id: str,
    task_id: str,
    wg_id: str,
    org_id: str,
    vm_id: str,
    pending_id: str,
    op_type: str,
    params: Mapping[str, Any],
    requested_at: str,
    idempotency_key: str | None,
    local_error: Mapping[str, Any] | None,
    actor_id: str | None,
) -> bool:
    """Record a journaled request in `received`; False when the operation ID is known already (a re-read page)."""
    cur = await conn.execute(
        "INSERT INTO runner.operation (id, session_id, task_id, wg_id, org_id, vm_id, pending_id, type, state, params,"
        " requested_at, idempotency_key, local_error, actor_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'received',"
        " %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (
            operation_id,
            session_id,
            task_id,
            wg_id,
            org_id,
            vm_id,
            pending_id,
            op_type,
            Jsonb(dict(params)),
            requested_at,
            idempotency_key,
            None if local_error is None else Jsonb(dict(local_error)),
            actor_id,
        ),
    )
    return cur.rowcount == 1


async def get(conn: AsyncConnection[Any], operation_id: str) -> Operation | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT + " WHERE id = %s", (operation_id,))
        found = await cur.fetchone()
    return None if found is None else _row(found)


async def update(conn: AsyncConnection[Any], operation_id: str, **fields: object) -> None:
    """Set columns of `COLUMNS` (JSON columns as JSON) and `updated_at`."""
    unknown = set(fields) - COLUMNS
    if unknown or not fields:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"unknown operation state: {fields['state']!r}")
    names = sorted(fields)
    values = [
        Jsonb(fields[name]) if name in JSON_COLUMNS and fields[name] is not None else fields[name] for name in names
    ]
    assignments = ", ".join(f"{name} = %s" for name in names)
    await conn.execute(
        f"UPDATE runner.operation SET {assignments}, updated_at = now() WHERE id = %s",
        (*values, operation_id),
    )


async def pending_work(conn: AsyncConnection[Any], answer_window_s: float) -> list[str]:
    """IDs of the operations the pipeline still has to work on, oldest first: open states, and final ones younger
    than `answer_window_s` whose answer the guest has not taken yet."""
    cur = await conn.execute(
        "SELECT id::text FROM runner.operation WHERE state IN ('received', 'accepted', 'executing')"
        " OR (answered_at IS NULL AND (answer IS NOT NULL OR state = 'duplicate')"
        " AND created_at > now() - make_interval(secs => %s)) ORDER BY created_at, id",
        (answer_window_s,),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def key_conflict(conn: AsyncConnection[Any], op: Operation, *, epoch: int, tag: str, window_s: int) -> bool:
    """Whether another operation of the task and type registered the idempotency key of `op` with another dedupe tag
    of the same epoch within `window_s` and holds its slot (`accepted|executing|succeeded`, as H5 of 03-api.md section
    8.3; KOR-28). Tags of different epochs cannot be compared (04-crypto.md section 4.10)."""
    cur = await conn.execute(
        "SELECT 1 FROM runner.operation WHERE task_id = %s AND type = %s AND idempotency_key = %s AND id <> %s"
        " AND state IN ('accepted', 'executing', 'succeeded')"
        " AND dedupe_tag IS NOT NULL AND dedupe_tag <> %s AND registration->'dedupe'->'epoch' = %s"
        " AND created_at > now() - make_interval(secs => %s) LIMIT 1",
        (op.task_id, op.type, op.idempotency_key, op.id, tag, Jsonb(epoch), window_s),
    )
    return await cur.fetchone() is not None


async def task_commits(conn: AsyncConnection[Any], task_id: str) -> set[str]:
    """Commit SHAs created by succeeded `github.commit` operations of the task on this device (`github.push`)."""
    cur = await conn.execute(
        "SELECT result->>'commit_sha' FROM runner.operation WHERE task_id = %s AND type = 'github.commit'"
        " AND state = 'succeeded' AND result ? 'commit_sha'",
        (task_id,),
    )
    return {str(row[0]) for row in await cur.fetchall()}


async def lock_session(conn: AsyncConnection[Any], session_id: str) -> None:
    """Serialise the holds of one session until the end of the transaction (flood limits, KOR-25)."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext('runner.operation.hold'), hashtext(%s))", (session_id,))


async def held_count(conn: AsyncConnection[Any], session_id: str) -> int:
    """Operations of the session held right now."""
    cur = await conn.execute(
        "SELECT count(*) FROM runner.operation WHERE session_id = %s AND state = 'received'"
        " AND held_until IS NOT NULL AND held_ended_at IS NULL",
        (session_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def last_waiting(conn: AsyncConnection[Any], session_id: str, reason: str) -> datetime | None:
    """When the session's last `waiting` event for `reason` was written (device wall clock)."""
    cur = await conn.execute(
        "SELECT max(held_notified_at) FROM runner.operation WHERE session_id = %s AND held_reason = %s",
        (session_id, reason),
    )
    row = await cur.fetchone()
    assert row is not None
    found: datetime | None = row[0]
    return found
