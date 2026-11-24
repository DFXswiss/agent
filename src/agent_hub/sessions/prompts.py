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
  connected model access (`agent_hub.sessions.model_retry`, ADR-079).
- **Next session.** When the device itself ends the newest main session of an open task (`stopped`, not after a
  user's "Stop", audit `session.control`) after it delivered a prompt, while prompts are pending there, `carry_over`
  creates the next main session at once, placed by the scheduler, which delivers them. A session the user ended
  starts nothing: its prompts wait for the next follow-up, whose new main session delivers them first.
- **Delivery.** A session enters `working` exactly when Claude Code accepted a prompt (the UserPromptSubmit hook,
  05-runner.md section 6.1 step 7 and 6.2 step 4), and prompts of one session are delivered strictly in event order. A
  prompt the guest refused (outbox audit item `session.prompt_refused`, 05-runner.md section 6.2 step 3) is consumed as
  well (it must never be delivered again, and the next one must not be taken for it). The first n prompts of a session
  are therefore done when it has n transitions into `working` plus refusals.

`pending_prompts` lists the undelivered prompts of live, not stopping sessions; `delivered_prompt_event_ids` the last
100 delivered prompt IDs of every listed session (a runner after a store wipe never delivers them again).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.crypto.ids import uuid7
from agent_hub.scheduler import load
from agent_hub.sessions.derive import STOPPED, STOPPING
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
RETRY_REASONS = (RETRY_REASON, MODEL_RETRY_REASON)


@dataclass(frozen=True)
class Prompt:
    """One prompt event of a task."""

    event_id: str
    scope_id: str
    scope_seq: int
    received_at: datetime


@dataclass(frozen=True)
class Routed:
    """The prompts of one main session."""

    task_id: str
    session_id: str
    delivered: tuple[Prompt, ...]
    pending: tuple[Prompt, ...]


def task_prompts(connection: Connection, task_ids: Sequence[UUID | str]) -> dict[str, list[Prompt]]:
    """The prompt events of the tasks in event order."""
    found: dict[str, list[Prompt]] = {str(task): [] for task in task_ids}
    for row in connection.execute(
        "SELECT task_id, id, scope_id, scope_seq, received_at FROM hub.event WHERE task_id = ANY(%s)"
        " AND kind = 'prompt' ORDER BY task_id, scope_seq",
        ([UUID(str(task)) for task in task_ids],),
    ):
        found[str(row[0])].append(Prompt(str(row[1]), str(row[2]), int(row[3]), row[4]))
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


def main_sessions(connection: Connection, task_ids: Sequence[UUID | str]) -> dict[str, list[Main]]:
    """The main sessions of the tasks, oldest first."""
    found: dict[str, list[Main]] = {str(task): [] for task in task_ids}
    for row in connection.execute(
        "SELECT s.task_id, s.id, s.created_at, (SELECT count(*) FROM hub.session_transition t"
        " WHERE t.session_id = s.id AND t.state = 'working') + (SELECT count(*) FROM hub.audit a"
        " WHERE a.session_id = s.id AND a.action = 'session.prompt_refused'), s.state,"
        " COALESCE((SELECT t.reason FROM hub.session_transition t WHERE t.session_id = s.id AND t.seq = 1) = ANY(%s),"
        " false) FROM hub.session s WHERE s.task_id = ANY(%s) AND s.depth = 0 ORDER BY s.task_id, s.created_at, s.id",
        (list(RETRY_REASONS), [UUID(str(task)) for task in task_ids]),
    ):
        found[str(row[0])].append(Main(str(row[1]), row[2], int(row[3]), row[4], bool(row[5])))
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
        carried = item.pending if main.state in CARRIED or (follower is not None and follower.retry) else ()
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
    create_main_session(connection, task, new_id, decision, created, actor=HUB, effects=effects)
