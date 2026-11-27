"""Test support of the attention suites (U-HUB-attention, KOR-39).

`Situation` wraps the operations helper (`tests/platform/hub/operations/support.py`: the tasks desk with an org, the
users `writer` (task owner), `reader`, `stranger`, the org device `device` assigned to `wg`, online and bound to the
org) and adds what the attention suites need: tasks created through the browser API and driven by the device (session
transitions, registered and finished operations), the stored attention record of a task, direct refreshes and rows
written directly for the read suites.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_hub.attention import store
from agent_hub.sessions.effects import Effects
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.events.support import Json
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import Actor, new_id

MAIN_SESSION_STATES = {"scheduling": "requested", "starting": "agent_starting", "closed": "stopped"}
"""The main session's state behind a task state of `Situation.insert_task` (the others are the same word)."""


@dataclass(frozen=True)
class Record:
    """The stored attention record of a task."""

    category: str
    reason: str
    ref_kind: str | None
    ref_id: str | None
    since: datetime
    seq: int
    subagents_total: int
    subagents_active: int

    @property
    def kind(self) -> tuple[str, str]:
        return self.category, self.reason


class Situation:
    """Tasks, sessions and operations of the scene and their attention records (module docstring)."""

    def __init__(self, ops: Ops) -> None:
        self.ops = ops

    @property
    def desk(self) -> Any:
        return self.ops.desk

    @property
    def scene(self) -> Any:
        return self.ops.scene

    @property
    def hub(self) -> Any:
        return self.ops.hub

    # --- records ---

    def record(self, task: str) -> Record | None:
        rows = self.desk.rows(
            "SELECT category, reason, ref_kind, ref_id::text, since, seq, subagents_total, subagents_active"
            " FROM hub.task_attention WHERE task_id = %s",
            task,
        )
        return Record(*rows[0]) if rows else None

    def kind(self, task: str) -> tuple[str, str] | None:
        found = self.record(task)
        return None if found is None else found.kind

    def refresh(self, task: str, *, flush: bool = True) -> bool:
        """`store.refresh` in its own transaction (effects sent after the commit unless `flush` is False)."""
        effects = Effects()
        with self.hub.pool.connection() as connection, connection.transaction():
            changed = store.refresh(connection, task, self.hub.clock(), effects)
        if flush:
            effects.flush()
        return changed

    # --- flows ---

    def started(self, actor: Actor | None = None) -> tuple[str, str]:
        """A task created through the browser API (placed on `device`, `starting`) and its main session."""
        task, session = self.desk.started(actor)
        self.scene.sessions[session] = (task, self.desk.wg)
        return task, session

    def working(self, actor: Actor | None = None) -> tuple[str, str]:
        """A started task whose main session reported `working`."""
        task, session = self.started(actor)
        self.desk.transition(session, "working")
        return task, session

    def step(self, session: str, state: str, reason: str | None = None) -> None:
        """A session transition one second later (device clock one tick behind the hub's)."""
        self.hub.clock.advance(1)
        self.desk.transition(session, state, reason)

    def ask(self, session: str, op_type: str = "human.question") -> str:
        """A question operation of the session, registered and finished `succeeded`."""
        self.hub.clock.advance(1)
        operation, answer = self.ops.decide(session, op_type)
        assert answer == {"state": "accepted"}
        self.ops.finish(operation)
        return operation

    def refused(self, session: str, op_type: str, code: str) -> str:
        """An operation the runner rejected locally with `code`."""
        self.hub.clock.advance(1)
        operation, answer = self.ops.decide(session, op_type, local_check="rejected", error_code=code)
        assert answer == {"state": "rejected", "error_code": code}
        return operation

    def succeeded(self, session: str, op_type: str) -> str:
        self.hub.clock.advance(1)
        operation, _answer = self.ops.decide(session, op_type)
        self.ops.finish(operation)
        return operation

    def prompt(self, task: str) -> None:
        self.hub.clock.advance(1)
        response = self.desk.prompt(self.scene.writer, task)
        assert response.status_code == 200, response.text

    # --- rows written directly (read suites) ---

    def insert_task(self, wg: str, owner: str, *, state: str = "working", at: datetime | None = None) -> str:
        """A task with its main session in the state that gives the task `state` (every task that is not captured
        has one, ADR-102)."""
        task = new_id()
        now = at or self.hub.clock()
        self.desk.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at,"
            " closed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            task,
            self.desk.org,
            wg,
            owner,
            state,
            now,
            now,
            now,
            now if state == "closed" else None,
        )
        session_state = MAIN_SESSION_STATES.get(state, state)
        self.desk.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, depth, state, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s)",
            new_id(),
            task,
            self.desk.org,
            wg,
            owner,
            None if session_state == "requested" else self.scene.device.id,
            session_state,
            now,
        )
        return task

    def insert_record(self, task: str, wg: str, owner: str, kind: tuple[str, str], since: datetime) -> None:
        category, reason = kind
        self.desk.execute(
            "INSERT INTO hub.task_attention (task_id, org_id, wg_id, owner_id, category, reason, since, seq,"
            " subagents_total, subagents_active, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 0, 0, %s)",
            task,
            self.desk.org,
            wg,
            owner,
            category,
            reason,
            since,
            since,
        )


def attention_frames(published: MemoryPublisher) -> list[tuple[str, Json]]:
    """`(wg_id, data)` of every `attention` frame (checked against its contract, always restricted to a workgroup)."""
    found = []
    for item in published.items():
        if item.frame != store.FRAME:
            continue
        assert item.audience.kind == "org"
        assert item.wg_id is not None
        contracts_registry().validate("ws/sse/attention.json", dict(item.data))
        found.append((str(item.wg_id), dict(item.data)))
    return found
