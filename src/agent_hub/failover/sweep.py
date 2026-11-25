"""Device failure: interrupted sessions and their move to another device (ADR-087; 03-api.md section 4.6.5).

A device has **failed** while it is offline (no fresh `device_presence` row) and `device.offline_since` lies
`AGENT_HUB_DEVICE_FAILOVER_SECONDS` or more back (`agent_hub.stream.presence` sets it). `sweep(context)` runs every
15 s in every hub process (job `failover.sweep`):

1. **Back in time.** An interrupted live session whose device is online again is no longer interrupted
   (`interruption_ended_at`, audit `session.interruption_ended`; locked `SKIP LOCKED`); its task derives as before.
   `interrupted_at` stays, so a restarted device that reports the session `failed` afterwards still starts its
   successor (`agent_hub.sessions.failover`).
2. **Each task with a live session on a failed device**, in task ID order, in a transaction of its own: the global
   audit lock first (as `operations.expire`), then the task row, the failed devices' rows and the task's live sessions,
   each `FOR UPDATE SKIP LOCKED` - a task, device or session another writer holds (a connecting device, a user's
   `control`) is taken at the next sweep, so the sweep never waits in a lock cycle and never moves a session of a
   device that is just connecting.
   - **Interrupt:** every active session there (start chain, `working`, `resuming`, `stopping`) without mark gets
     `interrupted_at` and the audit row `session.interrupted {offline_since}`; a task whose main session is
     interrupted derives to `scheduling/device_failed` (`agent_hub.sessions.derive`).
   - **Closed task, or main session elsewhere:** its sessions there end `stopped` with `device_failed` (no successor;
     the main session may ask for new subagents).
   - **Main session there:** `target` chooses another device; without one nothing more happens (the task waits, the
     next sweep tries again). With one, `move` ends every live session of the task there `stopped`/`device_failed`,
     ends their operations (`end_operations`) and creates the successor on the chosen device
     (`agent_hub.sessions.failover`).
   - Every device whose sessions ended gets the hint `reconcile` (dropped while it is offline): one that connects right
     after the commit stops them at once.

Every transaction sends its effects after the commit. The function answers the number of tasks moved.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.audit.chain import AUDIT_LOCK
from agent_hub.crypto.encoding import format_time
from agent_hub.db.pool import transaction
from agent_hub.operations import dedupe
from agent_hub.operations.steps import FINISHED_SIGNAL, OperationRef, add_transition, audit_step
from agent_hub.outbox.handlers_operation import operation_row
from agent_hub.scheduler import load
from agent_hub.scheduler.core import Placement, choose
from agent_hub.sessions import failover, rows
from agent_hub.sessions.derive import DEVICE_FAILED, INTERRUPTED_SQL, RESUMING, STARTUP, STOPPED, STOPPING, WORKING
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import HUB, TaskRow, audit, refresh_task
from agent_hub.sessions.transitions import SessionRow, record

Connection = psycopg.Connection[Any]
ACTIVE = frozenset({*STARTUP, WORKING, RESUMING, STOPPING})
"""Session states in which a session does something: these are interrupted (ADR-087 section 2)."""
INTERRUPTED = "session.interrupted"
INTERRUPTION_ENDED = "session.interruption_ended"
RUN_TYPE = "test.run"
RECONCILE_HINT = "reconcile"


@dataclass(frozen=True)
class Failed:
    """A failed device: its kind, owning organization (org devices) and since when it is offline."""

    device_id: str
    kind: str
    owner_org: str | None
    offline_since: datetime


@dataclass(frozen=True)
class Live:
    """A live session of a task (locked)."""

    id: UUID
    device_id: str | None
    state: str
    depth: int
    interrupted: bool
    triggered_by: UUID | None


def failed_devices(
    connection: Connection, now: datetime, failover_seconds: int, online_seconds: int, *, lock: bool = False
) -> dict[str, Failed]:
    """The failed devices by ID (module docstring); `lock` holds their rows `FOR UPDATE SKIP LOCKED`, so a device
    that is connecting (its connect holds the row) counts as not failed in this transaction."""
    found = connection.execute(
        "SELECT d.id, d.kind, d.owner_org, d.offline_since FROM hub.device d WHERE d.offline_since <= %s"
        " AND NOT EXISTS (SELECT 1 FROM hub.device_presence p WHERE p.device_id = d.id AND p.last_heartbeat_at > %s)"
        + (" ORDER BY d.id FOR UPDATE OF d SKIP LOCKED" if lock else ""),
        (now - timedelta(seconds=failover_seconds), load.online_after(now, online_seconds)),
    ).fetchall()
    return {str(row[0]): Failed(str(row[0]), row[1], None if row[2] is None else str(row[2]), row[3]) for row in found}


def _task(connection: Connection, task_id: UUID) -> TaskRow | None:
    row = connection.execute(
        "SELECT id, org_id, wg_id, owner_id, state, state_reason FROM hub.task WHERE id = %s FOR UPDATE SKIP LOCKED",
        (task_id,),
    ).fetchone()
    return None if row is None else TaskRow(*row)


def _live(connection: Connection, task_id: UUID) -> list[Live] | None:
    """The task's live sessions, locked; None when another transaction holds one of them (`control` locks a session
    without its task: the sweep never waits for it and takes the task at the next run)."""
    where = " FROM hub.session WHERE task_id = %s AND state NOT IN ('stopped', 'failed')"
    total = cast(tuple[int], connection.execute("SELECT count(*)" + where, (task_id,)).fetchone())[0]
    found = connection.execute(
        f"SELECT id, device_id, state, depth, {INTERRUPTED_SQL}, triggered_by"
        + where
        + " ORDER BY created_at, id FOR UPDATE SKIP LOCKED",
        (task_id,),
    ).fetchall()
    if total != len(found):
        return None
    return [
        Live(row[0], None if row[1] is None else str(row[1]), row[2], row[3], bool(row[4]), row[5]) for row in found
    ]


def _newest_main(connection: Connection, task_id: UUID) -> UUID:
    row = connection.execute(
        "SELECT id FROM hub.session WHERE task_id = %s AND depth = 0 ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return UUID(str(row[0])) if row is not None else UUID(int=0)


def _frame(connection: Connection, task: TaskRow, session_id: UUID, effects: Effects) -> None:
    effects.org_frame(task.org_id, "session", rows.session_frame(connection, session_id))


def interrupt(
    connection: Connection, task: TaskRow, session: Live, failed: Failed, *, now: datetime, effects: Effects
) -> None:
    """Mark an active session of a failed device (module docstring)."""
    connection.execute("UPDATE hub.session SET interrupted_at = %s WHERE id = %s", (now, session.id))
    audit(
        connection,
        HUB,
        INTERRUPTED,
        task,
        now,
        detail={"offline_since": format_time(failed.offline_since)},
        session_id=session.id,
        device_id=UUID(failed.device_id),
        target=("session", session.id),
    )
    _frame(connection, task, session.id, effects)


def end(connection: Connection, task: TaskRow, session: Live, now: datetime, effects: Effects) -> None:
    """End a session of a failed device `stopped` with `device_failed` (the hub's own step, no task derivation)."""
    row = SessionRow(session.id, task.id, None if session.device_id is None else UUID(session.device_id), session.state)
    record(
        connection, task, row, STOPPED, DEVICE_FAILED, at=now, received_at=now, actor=HUB, effects=effects, derive=False
    )


def end_operations(connection: Connection, session_ids: list[UUID], now: datetime, effects: Effects) -> int:
    """The operations of the ended sessions (ADR-087 section 4): every one `accepted` or `executing` ends `failed` with
    `device_failed` and frees its dedupe slot (its outcome on the lost device is unknown; a test run is cancelled), and
    the slots of their `subagent.request` and `human.*` operations are freed (their results belong to the lost
    session). Rows another transaction holds are left alone. The number of test runs ended."""
    ids = [str(item) for item in session_ids]
    found = connection.execute(
        "SELECT id, org_id, wg_id, task_id, session_id, device_id, type, github_identity, triggered_by"
        " FROM hub.operation WHERE session_id = ANY(%s::uuid[]) AND state IN ('accepted', 'executing')"
        " ORDER BY created_at, id FOR UPDATE SKIP LOCKED",
        (ids,),
    ).fetchall()
    for row in found:
        ref = replace(OperationRef(*row[:7]), github_identity=row[7], triggered_by=row[8])
        connection.execute(
            "UPDATE hub.operation SET state = 'failed', error_code = %s, finished_at = %s WHERE id = %s",
            (DEVICE_FAILED, now, ref.id),
        )
        add_transition(connection, ref.id, "failed", DEVICE_FAILED, at=now, by_id=None)
        dedupe.release(connection, ref.id)
        audit_step(connection, ref, "failed", now, actor_kind="hub", actor_id=None, error_code=DEVICE_FAILED)
        effects.org_frame(ref.org_id, "operation", operation_row(connection, ref.id))
        effects.signal(
            FINISHED_SIGNAL,
            operation_id=ref.id,
            session_id=ref.session_id,
            task_id=ref.task_id,
            org_id=ref.org_id,
            state="failed",
        )
    for (operation_id,) in connection.execute(
        "SELECT id FROM hub.operation WHERE session_id = ANY(%s::uuid[])"
        " AND (type = 'subagent.request' OR type LIKE 'human.%%') ORDER BY id",
        (ids,),
    ).fetchall():
        dedupe.release(connection, operation_id)
    return sum(1 for row in found if row[6] == RUN_TYPE)


def target(
    connection: Connection, task: TaskRow, failed: Failed, now: datetime, online_seconds: int
) -> Placement | None:
    """The device the task moves to, None while there is none (ADR-087 section 3, points 1-2): the scheduler's
    candidates for the task without the failed device, only devices of the same organization when the failed one is
    an organization's own, decided by `choose`."""
    found = load.inputs(
        connection, task.wg_id, now, online_seconds, pin=load.task_pin(connection, task.id), owner=task.owner_id
    )
    others = [item for item in found.candidates if item.device_id != failed.device_id]
    if failed.kind == "org" and others:
        own = {
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM hub.device WHERE id = ANY(%s::uuid[]) AND kind = 'org' AND owner_org = %s",
                ([item.device_id for item in others], failed.owner_org),
            )
        }
        others = [item for item in others if item.device_id in own]
    if not others:
        return None
    decision = choose(others, found.allowance, pinned=found.pinned)
    return decision if isinstance(decision, Placement) else None


def move(
    connection: Connection,
    task: TaskRow,
    main: Live,
    lost: list[Live],
    *,
    placement: Placement,
    now: datetime,
    effects: Effects,
) -> UUID | None:
    """End the task's sessions on the failed device and create the successor (`agent_hub.sessions.failover`); its
    ID, None when none is due."""
    for session in lost:
        end(connection, task, session, now, effects)
    end_operations(connection, [session.id for session in lost], now, effects)
    return failover.successor(
        connection,
        task,
        main.id,
        was=main.state,
        triggered_by=main.triggered_by,
        decision=placement,
        now=now,
        effects=effects,
    )


def handle(connection: Connection, task_id: UUID, now: datetime, settings: tuple[int, int], effects: Effects) -> bool:
    """One task with live sessions on failed devices (module docstring, point 2); whether it moved. `settings` are
    the failover and online seconds; the failed devices are read again under the locks (a device that reconnected
    or is connecting since the start of the sweep is no longer failed)."""
    failover_seconds, online = settings
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
    task = _task(connection, task_id)
    if task is None:
        return False
    failed = failed_devices(connection, now, failover_seconds, online, lock=True)
    live = _live(connection, task_id)
    lost = [item for item in live or () if item.device_id in failed]
    if not lost:
        return False
    fresh = [item for item in lost if item.state in ACTIVE and not item.interrupted]
    for session in fresh:
        interrupt(connection, task, session, failed[str(session.device_id)], now=now, effects=effects)
    newest = _newest_main(connection, task_id)
    main = next((item for item in lost if item.id == newest), None)
    ended: list[Live] = []
    moved = False
    if task.state == "closed" or main is None:
        ended = lost
        for session in lost:
            end(connection, task, session, now, effects)
        end_operations(connection, [session.id for session in lost], now, effects)
    else:
        placement = target(connection, task, failed[str(main.device_id)], now, online)
        if placement is not None:
            ended = [item for item in lost if item.device_id == main.device_id]
            move(connection, task, main, ended, placement=placement, now=now, effects=effects)
            moved = True
    for device in sorted({str(item.device_id) for item in ended}):
        # a device that connects right after the commit learns at once that these sessions ended
        effects.hint(device, RECONCILE_HINT, {})
    if fresh or ended:
        # an unchanged task is not touched (no activity, no frame) at every sweep
        refresh_task(connection, task, now, HUB, effects)
    return moved


def resume(connection: Connection, task_id: UUID, now: datetime, online: int, effects: Effects) -> None:
    """Clear the marks of the task's live sessions whose device is online again (module docstring, point 1)."""
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (AUDIT_LOCK,))
    task = _task(connection, task_id)
    if task is None:
        return
    found = connection.execute(
        f"SELECT s.id, s.device_id FROM hub.session s WHERE s.task_id = %s AND {INTERRUPTED_SQL}"
        " AND s.state NOT IN ('stopped', 'failed') AND EXISTS (SELECT 1 FROM hub.device_presence p"
        " WHERE p.device_id = s.device_id AND p.last_heartbeat_at > %s) ORDER BY s.id FOR UPDATE OF s SKIP LOCKED",
        (task_id, load.online_after(now, online)),
    ).fetchall()
    back = [(UUID(str(row[0])), row[1]) for row in found]
    if back:
        connection.execute(
            "UPDATE hub.session SET interruption_ended_at = %s WHERE id = ANY(%s::uuid[])",
            (now, [str(item[0]) for item in back]),
        )
    for session_id, device_id in back:
        audit(
            connection,
            HUB,
            INTERRUPTION_ENDED,
            task,
            now,
            detail={},
            session_id=session_id,
            device_id=device_id,
            target=("session", session_id),
        )
        _frame(connection, task, session_id, effects)
    if back:
        refresh_task(connection, task, now, HUB, effects)


def _tasks(connection: Connection, sql: str, params: tuple[Any, ...]) -> list[UUID]:
    return [UUID(str(row[0])) for row in connection.execute(sql, params).fetchall()]


def sweep(context: Any) -> int:
    """One pass (module docstring); the number of tasks moved."""
    online = context.config.device_online_seconds
    with transaction(context.pool) as connection:
        now = context.clock()
        back = _tasks(
            connection,
            "SELECT DISTINCT s.task_id FROM hub.session s JOIN hub.device_presence p ON p.device_id = s.device_id"
            f" WHERE {INTERRUPTED_SQL} AND s.state NOT IN ('stopped', 'failed') AND p.last_heartbeat_at > %s"
            " ORDER BY s.task_id",
            (load.online_after(now, online),),
        )
        failed = failed_devices(connection, now, context.config.device_failover_seconds, online)
        affected = _tasks(
            connection,
            "SELECT DISTINCT task_id FROM hub.session WHERE device_id = ANY(%s::uuid[])"
            " AND state NOT IN ('stopped', 'failed') ORDER BY task_id",
            (sorted(failed),),
        )
    for task_id in back:
        effects = Effects()
        with transaction(context.pool) as connection:
            resume(connection, task_id, context.clock(), online, effects)
        effects.flush()
    moved = 0
    settings = (context.config.device_failover_seconds, online)
    for task_id in affected:
        effects = Effects()
        with transaction(context.pool) as connection:
            moved += handle(connection, task_id, context.clock(), settings, effects)
        effects.flush()
    return moved
