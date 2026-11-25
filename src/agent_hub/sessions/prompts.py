"""Which prompt belongs to which main session and which ones were delivered (03-api.md sections 4.6.3, 6.1).

The hub keeps no prompt table: everything is derived from rows it already has.

- **Routing.** Prompts address the task (`prompt` events with the task's ID, ordered by `scope_seq`). A prompt belongs
  to the main session that was current when the hub received it: the newest session of depth 0 with `created_at <=
  received_at` (a new main session and the prompt that caused it are stored in one transaction with the same hub time;
  `agent_hub.tasks.service.add_prompt` serialises the follow-ups of a task and reads the hub time after that, later
  than the newest main session, so a prompt is never received before a session a concurrent follow-up created).
  **Carry-over (KOR-11, NA-04).** A main session that ended `stopping`/`stopped` hands the prompts it did not deliver
  to the next main session of the task (before that session's own prompts, in event order), so a follow-up routed to a
  session that the device ends in the same instant is not lost. A `failed` session keeps them: the task shows the
  failure with "Retry" (06-web.md section 4.4), which sends the newest prompt again, and that must not be delivered
  twice - except for a session the device refused with `capacity_exhausted`: the hub's next attempt (a main session
  whose first transition `requested` carries that reason, `agent_hub.sessions.retry`) takes them over (KOR-14); the
  same holds for a session that failed `model_access_unavailable` and the attempt the hub creates once the owner
  connected model access (`agent_hub.sessions.model_retry`, ADR-079), and for the successor after a device failure
  (first transition `requested` with `device_failed`, `agent_hub.sessions.failover`). **Device failure (ADR-086
  section 3):** a lost session whose turn was running hands on the prompt stored as its `redeliver_event_id` as well,
  first: the interrupted turn did not finish, so its successor gets that instruction again.
- **Next session.** When the device itself ends the newest main session of an open task (`stopped`, not after a
  user's "Stop", audit `session.control`) after it delivered a prompt, while prompts are pending there, `carry_over`
  creates the next main session at once, placed by the scheduler, which delivers them. A session the user ended
  starts nothing: its prompts wait for the next follow-up, whose new main session delivers them first.
- **Delivery.** A session enters `working` exactly when Claude Code accepted a prompt (the UserPromptSubmit hook,
  05-runner.md section 6.1 step 7 and 6.2 step 4), and prompts of one session are delivered strictly in event order. A
  prompt the guest refused (outbox audit item `session.prompt_refused`, 05-runner.md section 6.2 step 3) is consumed as
  well (it must never be delivered again, and the next one must not be taken for it). The first n prompts of a session
  are therefore done when it has n transitions into `working` plus refusals.
- **Withdrawal (KOR-46, ADR-083 section 3).** A prompt whose withdrawal is confirmed (`hub.prompt_withdrawal`
  `withdrawn_at`: at once for a session without a device, else when the device reported `session.prompt_withdrawn`)
  leaves the routing by its event ID: it never counts as delivered and the rest keep their order. The device reports
  a withdrawal through the same ordered outbox as the transitions, so the hub learns of it before the acceptance of a
  later prompt. A prompt that is pending in several sessions (the ended session and the one it was carried to) is
  **held** by the last of them (`holders`).

`pending_prompts` lists the undelivered prompts of live, not stopping sessions; `delivered_prompt_event_ids` the last
100 delivered prompt IDs of every listed session (a runner after a store wipe never delivers them again).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.ids import uuid7
from agent_hub.scheduler import load
from agent_hub.sessions.derive import DEVICE_FAILED, STOPPED, STOPPING
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.lifecycle import create_main_session
from agent_hub.sessions.taskstate import HUB, TaskRow
from agent_hub.usage import binding

Connection = psycopg.Connection[Any]
DELIVERED_KEEP = 100
TICK = timedelta(microseconds=1)
CARRIED = frozenset({STOPPING, STOPPED})
"""End states whose undelivered prompts go to the next main session."""
RETRY_REASON = "capacity_exhausted"
"""The reason of the first transition (`requested`) of the hub's attempt after a refusal for lack of capacity."""
MODEL_RETRY_REASON = "model_access_unavailable"
"""The reason of the first transition of the hub's attempt after the owner connected model access (ADR-079)."""
RETRY_REASONS = (RETRY_REASON, MODEL_RETRY_REASON, DEVICE_FAILED)
"""First-transition reasons of a main session that takes over the undelivered prompts of the one before it, also when
that one `failed`: the hub's attempts and the successor after a device failure (ADR-086 section 3)."""


@dataclass(frozen=True)
class Prompt:
    """One prompt event of a task."""

    event_id: str
    scope_id: str
    scope_seq: int
    received_at: datetime
    author_id: str | None = None
    """The verified author of the prompt event (ADR-083 section 2: the person a new main session is created for)."""


@dataclass(frozen=True)
class Routed:
    """The prompts of one main session."""

    task_id: str
    session_id: str
    delivered: tuple[Prompt, ...]
    pending: tuple[Prompt, ...]


def task_prompts(connection: Connection, task_ids: Sequence[UUID | str]) -> dict[str, list[Prompt]]:
    """The prompt events of the tasks in event order, without the withdrawn ones (module docstring)."""
    found: dict[str, list[Prompt]] = {str(task): [] for task in task_ids}
    for row in connection.execute(
        "SELECT e.task_id, e.id, e.scope_id, e.scope_seq, e.received_at, e.author_id FROM hub.event e"
        " WHERE e.task_id = ANY(%s) AND e.kind = 'prompt' AND NOT EXISTS (SELECT 1 FROM hub.prompt_withdrawal w"
        " WHERE w.event_id = e.id AND w.withdrawn_at IS NOT NULL) ORDER BY e.task_id, e.scope_seq",
        ([UUID(str(task)) for task in task_ids],),
    ):
        found[str(row[0])].append(Prompt(str(row[1]), str(row[2]), int(row[3]), row[4], str(row[5])))
    return found


@dataclass(frozen=True)
class Main:
    """What the routing needs of one main session."""

    session_id: str
    created_at: datetime
    done: int
    """Prompts done: transitions into `working` plus refusals."""
    state: str
    retry: bool = False
    """The hub's next attempt after a refusal for lack of capacity (`agent_hub.sessions.retry`) or after the owner
    connected model access (`agent_hub.sessions.model_retry`, ADR-079)."""
    redeliver: str | None = None
    """The prompt the hub hands to its successor again after a device failure (`session.redeliver_event_id`, ADR-086
    section 3): before the undelivered ones."""


def main_sessions(connection: Connection, task_ids: Sequence[UUID | str]) -> dict[str, list[Main]]:
    """The main sessions of the tasks, oldest first."""
    found: dict[str, list[Main]] = {str(task): [] for task in task_ids}
    for row in connection.execute(
        "SELECT s.task_id, s.id, s.created_at, (SELECT count(*) FROM hub.session_transition t"
        " WHERE t.session_id = s.id AND t.state = 'working') + (SELECT count(*) FROM hub.audit a"
        " WHERE a.session_id = s.id AND a.action = 'session.prompt_refused'), s.state,"
        " COALESCE((SELECT t.reason FROM hub.session_transition t WHERE t.session_id = s.id AND t.seq = 1) = ANY(%s),"
        " false), s.redeliver_event_id"
        " FROM hub.session s WHERE s.task_id = ANY(%s) AND s.depth = 0 ORDER BY s.task_id, s.created_at, s.id",
        (list(RETRY_REASONS), [UUID(str(task)) for task in task_ids]),
    ):
        again = None if row[6] is None else str(row[6])
        found[str(row[0])].append(Main(str(row[1]), row[2], int(row[3]), row[4], bool(row[5]), again))
    return found


def route(prompts: Sequence[Prompt], sessions: Sequence[Main], task_id: str) -> list[Routed]:
    """Split the prompts of one task among its main sessions (module docstring)."""
    routed: list[Routed] = []
    carried: tuple[Prompt, ...] = ()
    for index, main in enumerate(sessions):
        follower = sessions[index + 1] if index + 1 < len(sessions) else None
        until = None if follower is None else follower.created_at
        own = [p for p in prompts if p.received_at >= main.created_at and (until is None or p.received_at < until)]
        mine = [*carried, *own]
        item = Routed(task_id, main.session_id, tuple(mine[: main.done]), tuple(mine[main.done :]))
        routed.append(item)
        again = tuple(prompt for prompt in item.delivered if prompt.event_id == main.redeliver)
        carried = (*again, *item.pending) if main.state in CARRIED or (follower is not None and follower.retry) else ()
    return routed


def routing(connection: Connection, task_ids: Sequence[UUID | str]) -> dict[str, Routed]:
    """The routed prompts of every main session of the tasks, by session ID."""
    prompts = task_prompts(connection, task_ids)
    sessions = main_sessions(connection, task_ids)
    found: dict[str, Routed] = {}
    for task in prompts:
        for item in route(prompts[task], sessions[task], task):
            found[item.session_id] = item
    return found


def holders(found: Mapping[str, Routed]) -> dict[str, tuple[Prompt, Routed]]:
    """Each pending prompt of `found` (a `routing` result) with the main session that holds it, by event ID: the last
    one in route order that lists it as pending (module docstring, "Withdrawal")."""
    held: dict[str, tuple[Prompt, Routed]] = {}
    for item in found.values():
        for prompt in item.pending:
            held[prompt.event_id] = (prompt, item)
    return held


def was_delivered(found: Mapping[str, Routed], event_id: str) -> bool:
    """The prompt was delivered by some main session of `found` (a `routing` result): one that is pending again after
    a device failure (ADR-086 section 3) can no longer be withdrawn."""
    return any(prompt.event_id == event_id for item in found.values() for prompt in item.delivered)


def via_agent(connection: Connection, event_ids: Sequence[str]) -> set[str]:
    """The prompts among `event_ids` the hub received from their author's agent: the audit row `event.append` of the
    event names the agent (`agent_id`, ADR-056; ADR-083 section 1)."""
    found = connection.execute(
        "SELECT DISTINCT event_id FROM hub.audit WHERE event_id = ANY(%s::uuid[]) AND action = 'event.append'"
        " AND agent_id IS NOT NULL",
        (list(event_ids),),
    ).fetchall()
    return {str(row[0]) for row in found}


def withdrawing(connection: Connection, event_ids: Sequence[str]) -> set[str]:
    """The prompts among `event_ids` whose withdrawal is requested and not yet confirmed (`hub.prompt_withdrawal`)."""
    found = connection.execute(
        "SELECT event_id FROM hub.prompt_withdrawal WHERE event_id = ANY(%s::uuid[]) AND withdrawn_at IS NULL",
        (list(event_ids),),
    ).fetchall()
    return {str(row[0]) for row in found}


def first_author(pending: Sequence[Prompt]) -> UUID:
    """The author of the first of the routed prompts (`task_prompts`) a new main session takes over: its
    `triggered_by` (ADR-083 section 2)."""
    return UUID(str(pending[0].author_id))


def device_pending(connection: Connection, device_id: UUID | str) -> list[str]:
    """The event IDs of the prompts pending at a device: those of its main sessions that are neither `stopping` nor
    ended, in session and event order (as `pending_prompts` of `GET /v1/device/state` lists them)."""
    sessions = connection.execute(
        "SELECT id, task_id FROM hub.session WHERE device_id = %s AND depth = 0"
        " AND state NOT IN ('stopping', 'stopped', 'failed') ORDER BY created_at, id",
        (device_id,),
    ).fetchall()
    found = routing(connection, sorted({str(row[1]) for row in sessions}))
    return [prompt.event_id for row in sessions for prompt in found[str(row[0])].pending]


def device_holder(connection: Connection, device_id: UUID | str, task_id: UUID | str, event_id: str) -> str | None:
    """The main session of the device that holds the prompt undelivered now (it is pending there and the session is
    neither `stopping` nor ended: exactly when `GET /v1/device/state` lists it in that device's `pending_prompts`),
    None otherwise."""
    sessions = connection.execute(
        "SELECT id FROM hub.session WHERE device_id = %s AND task_id = %s AND depth = 0"
        " AND state NOT IN ('stopping', 'stopped', 'failed') ORDER BY created_at, id",
        (device_id, task_id),
    ).fetchall()
    found = routing(connection, [task_id])
    for row in sessions:
        if any(prompt.event_id == event_id for prompt in found[str(row[0])].pending):
            return str(row[0])
    return None


def session_of(connection: Connection, task_id: UUID | str, received_at: datetime) -> str | None:
    """The main session a prompt received at `received_at` belongs to."""
    row = connection.execute(
        "SELECT id FROM hub.session WHERE task_id = %s AND depth = 0 AND created_at <= %s"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, received_at),
    ).fetchone()
    return None if row is None else str(row[0])


def carry_over(connection: Connection, task: TaskRow, session_id: UUID | str, now: datetime, effects: Effects) -> None:
    """After the device ended main session `session_id` of the locked task `stopped`: the next main session when the
    ended one is the newest, the task is open, no user asked to stop it, it delivered a prompt (it ran; a
    session the device ends during its start, e.g. after an unassignment, never restarts itself, so a device that keeps
    refusing a workgroup cannot cause a chain of sessions) and prompts are pending there (module docstring, "Next
    session"); the session is placed by the scheduler like a follow-up's."""
    if task.state == "closed":
        return
    newest = cast(
        tuple[UUID],
        connection.execute(
            "SELECT id FROM hub.session WHERE task_id = %s AND depth = 0 ORDER BY created_at DESC, id DESC LIMIT 1",
            (task.id,),
        ).fetchone(),
    )
    held = cast(
        tuple[bool],
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM hub.audit WHERE session_id = %s AND action = 'session.control'"
            " AND detail->>'action' = 'stop')",
            (session_id,),
        ).fetchone(),
    )[0]
    if str(newest[0]) != str(session_id) or held:
        return
    ended = routing(connection, [task.id])[str(session_id)]
    if not ended.pending or not ended.delivered:
        return
    # after every prompt of the task (all of them belong to the ended session, the newest), so they stay carried ones
    created = max(now, ended.pending[-1].received_at + TICK)
    decision = load.decide(
        connection,
        task.wg_id,
        created,
        binding.online_window(),
        pin=load.task_pin(connection, task.id),
        owner=task.owner_id,
    )
    new_id = UUID(uuid7(int(created.timestamp() * 1000)))
    create_main_session(
        connection,
        task,
        new_id,
        decision,
        created,
        actor=HUB,
        effects=effects,
        triggered_by=first_author(ended.pending),
    )
