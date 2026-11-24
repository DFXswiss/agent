"""U-HUB-notifications (other rules): `task.idle` after at least 10 s of `working`, `task.failed` and
`invite.accepted`, each from the signal of its area, written once and sent as a `notification` frame (03-api.md
section 4.9); owed rotations notify nobody (KOR-34)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_hub import signals as hub_signals
from agent_hub.notifications import rules, signals
from agent_hub.sessions.transitions import apply
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.orgs.support import new_id, new_invite_id
from tests.platform.hub.whiteboards.support import Board


def _notifications(board: Board, kind: str) -> list[tuple[Any, ...]]:
    return board.rows(
        "SELECT recipient_id::text, org_id::text, wg_id::text, task_id::text, ref, created_at FROM hub.notification"
        " WHERE kind = %s ORDER BY created_at, id",
        kind,
    )


def _frames(recorder: publish.MemoryPublisher) -> list[tuple[str, str]]:
    return [(str(item.audience.id), item.data["kind"]) for item in recorder.items() if item.frame == "notification"]


def _transitions(hub: HubHarness, session: str, *steps: tuple[str, datetime]) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        for seq, (state, at) in enumerate(steps, start=1):
            connection.execute(
                "INSERT INTO hub.session_transition (session_id, seq, state, at, received_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (session, seq, state, at, at),
            )


def _emit(name: str, **ids: Any) -> None:
    assert hub_signals.emit(name, **ids) == 0
    signals.wait()


@pytest.mark.parametrize(("seconds", "notified"), [(10.0, True), (9.999, False), (60.0, True)])
@pytest.mark.usefixtures("bound")
def test_task_idle_after_ten_seconds_of_working(
    board: Board, hub: HubHarness, published: publish.MemoryPublisher, seconds: float, notified: bool
) -> None:
    scene = board.scene
    task, session = scene.start_session()
    start = hub.clock()
    _transitions(hub, session, ("working", start))
    received = hub.clock.advance(seconds + 1)
    with hub.pool.connection() as connection, connection.transaction():
        applied = apply(
            connection,
            session,
            "idle",
            None,
            start + timedelta(seconds=seconds),
            received_at=received,
            reported_by=scene.device.id,
        )
    applied.effects.flush()  # task.state idle
    signals.wait()
    expected = [(scene.writer.id, scene.org, scene.wg, task, None, received)] if notified else []
    assert _notifications(board, rules.TASK_IDLE) == expected
    assert _frames(published) == ([(scene.writer.id, rules.TASK_IDLE)] if notified else [])
    _emit("task.state", task_id=UUID(task), state="idle")  # again: written once
    assert len(_notifications(board, rules.TASK_IDLE)) == len(expected)


def test_task_idle_needs_working_right_before(board: Board, hub: HubHarness, bound: None) -> None:
    scene = board.scene
    task, session = scene.start_session()
    start = hub.clock()
    _transitions(hub, session, ("prompt_delivering", start), ("idle", start + timedelta(seconds=30)))
    _emit("task.state", task_id=UUID(task), state="idle")
    assert _notifications(board, rules.TASK_IDLE) == []
    _emit("task.state", task_id=UUID(task), state="working")
    _emit("task.state", task_id=UUID(new_id()), state="failed")  # unknown task
    assert board.rows("SELECT count(*) FROM hub.notification") == [(0,)]


def test_task_failed_notifies_the_owner_once(
    board: Board, hub: HubHarness, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    task, _session = scene.start_session()
    failed_at = hub.clock.advance(3)
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.task SET state = 'failed', state_reason = 'oom' WHERE id = %s", (task,))
        connection.execute(
            "INSERT INTO hub.task_transition (task_id, seq, state, reason, at) VALUES (%s, 1, 'failed', 'oom', %s)",
            (task, failed_at),
        )
    _emit("task.state", task_id=UUID(task), state="failed")
    _emit("task.state", task_id=UUID(task), state="failed")
    assert _notifications(board, rules.TASK_FAILED) == [(scene.writer.id, scene.org, scene.wg, task, None, failed_at)]
    assert _frames(published) == [(scene.writer.id, rules.TASK_FAILED)]


def test_task_rules_skip_an_owner_without_access(board: Board, hub: HubHarness, bound: None) -> None:
    scene = board.scene
    task, _session = scene.start_session()
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.task SET owner_id = %s WHERE id = %s", (board.outsider.id, task))
        connection.execute(
            "INSERT INTO hub.task_transition (task_id, seq, state, at) VALUES (%s, 1, 'failed', %s)",
            (task, hub.clock()),
        )
    _emit("task.state", task_id=UUID(task), state="failed")
    assert _notifications(board, rules.TASK_FAILED) == []
    with hub.pool.connection() as connection:
        assert rules.task_rule(connection, UUID(task), "idle") == []


def _accepted_invite(board: Board, hub: HubHarness) -> str:
    """The org invitation row of the outsider's accept, created by the admin (as the invitations area stores it)."""
    invite, now = new_invite_id(), hub.clock()
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute(
            "INSERT INTO hub.invite (id, scope, kind, org_id, log_seq, created_by, created_at, expires_at, used_at,"
            " used_by) VALUES (%s, 'org', 'user', %s, 1, %s, %s, %s, %s, %s)",
            (invite, board.scene.org, board.scene.admin.id, now, now + timedelta(hours=1), now, board.outsider.id),
        )
    return invite


def test_invite_accepted_notifies_the_inviting_admin(
    board: Board, hub: HubHarness, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    invite = _accepted_invite(board, hub)
    ids = {"org_id": UUID(scene.org), "invite_id": invite, "subject_id": UUID(board.outsider.id)}
    _emit("invite.accepted", **ids)
    _emit("invite.accepted", **ids)
    assert _notifications(board, rules.INVITE_ACCEPTED) == [
        (scene.admin.id, scene.org, None, None, board.outsider.id, hub.clock())
    ]
    assert _frames(published) == [(scene.admin.id, rules.INVITE_ACCEPTED)]
    with hub.pool.connection() as connection:
        assert rules.invite_rule(connection, UUID(scene.org), invite, UUID(scene.admin.id), hub.clock()) == []
        assert rules.invite_rule(connection, UUID(scene.org), "unknown", UUID(board.outsider.id), hub.clock()) == []


def _pending(hub: HubHarness, org: str, scopes: list[dict[str, str]]) -> None:
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.org SET rotation_pending = %s WHERE id = %s", (json.dumps(scopes), org))


def test_an_owed_rotation_notifies_nobody(
    board: Board, hub: HubHarness, bound: None, published: publish.MemoryPublisher
) -> None:
    """KOR-34 (ADR-058 section 3): owed rotations run silently; neither a log append nor a decertification of a device
    writes a `rotation.required` reminder any more."""
    scene = board.scene
    _pending(hub, scene.org, [{"scope": "wg", "scope_id": scene.wg}])
    _emit("log.appended", org_id=UUID(scene.org), seq=1)
    _emit("device.decertified", device=UUID(scene.device.id))
    assert _notifications(board, rules.ROTATION_REQUIRED) == []
    assert _frames(published) == []


def test_a_failed_main_session_notifies_through_the_sessions_area(
    board: Board, hub: HubHarness, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    task, session = scene.start_session()
    received = hub.clock.advance(2)
    with hub.pool.connection() as connection, connection.transaction():
        applied = apply(
            connection, session, "failed", "oom", received, received_at=received, reported_by=scene.device.id
        )
    applied.effects.flush()  # the real producer of task.state
    signals.wait()
    assert _notifications(board, rules.TASK_FAILED) == [(scene.writer.id, scene.org, scene.wg, task, None, received)]
    assert _frames(published) == [(scene.writer.id, rules.TASK_FAILED)]
