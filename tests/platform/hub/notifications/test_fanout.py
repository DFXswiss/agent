"""U-HUB-notifications (whiteboard fan-out): `notify:true` on a new entry gives every reader of its level except the
author a content-free `notification` row and frame, every active session in its scope a durable `session_ping` row
and its device one `ping` hint, and the audit row `whiteboard.notify` (03-api.md sections 4.9, 6.1, 6.2; ADR-020)."""

from __future__ import annotations

import uuid
from typing import Any

from agent_hub.notifications import signals
from agent_hub.stream import publish
from tests.platform.hub.conftest import HubHarness, contracts_registry
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.whiteboards.support import Board, id_of, ref_of


def _recipients(board: Board, ref: str) -> set[str]:
    return {str(row[0]) for row in board.rows("SELECT recipient_id FROM hub.notification WHERE ref = %s", ref)}


def _pinged(board: Board, ref: str) -> set[str]:
    return {str(row[0]) for row in board.rows("SELECT session_id FROM hub.session_ping WHERE ref = %s", ref)}


def _frames(recorder: publish.MemoryPublisher, frame: str) -> list[publish.Published]:
    """The published items of one type, each validated against its contract (SSE frame or device hint)."""
    found = [item for item in recorder.items() if item.frame == frame]
    for item in found:
        if item.audience.kind == "device":
            contracts_registry().validate("ws/device/hint.json", {"type": item.frame, **item.data})
        else:
            contracts_registry().validate("ws/sse/frame.json", {"event": item.frame, "data": dict(item.data)})
    return found


def _session(
    scene: Scene, *, wg: str | None = None, device: Actor | None = None, state: str = "working", **kw: Any
) -> tuple[str, str]:
    task, session = scene.start_session(device, wg=wg, **kw)
    if state != "working":
        with scene.world.hub.pool.connection() as connection, connection.transaction():
            connection.execute("UPDATE hub.session SET state = %s WHERE id = %s", (state, session))
    return task, session


def test_a_workgroup_entry_notifies_its_readers_and_pings_its_sessions(
    board: Board, bound: None, published: publish.MemoryPublisher, hub: HubHarness
) -> None:
    scene = board.scene
    _task, working = _session(scene)
    _session(scene, state="stopped")
    _session(scene, state="stopping")
    _session(scene, state="failed")
    _task, waiting = _session(scene, placed=False)
    _task, idle_other = _session(scene, device=scene.other_device, state="idle")
    _session(scene, wg=board.other_wg)
    entry = board.posted(scene.writer, board.entry(scene.writer), notify=[True])[0]
    ref = ref_of(entry)
    signals.wait()
    assert _recipients(board, ref) == {scene.admin.id, scene.reader.id}  # not the author, outsider or stranger
    assert _pinged(board, ref) == {working, waiting, idle_other}
    rows = board.rows(
        "SELECT kind, org_id, wg_id, task_id, created_at, read_at FROM hub.notification WHERE ref = %s", ref
    )
    assert {(row[0], str(row[1]), str(row[2]), row[3], row[4], row[5]) for row in rows} == {
        ("whiteboard.entry", scene.org, scene.wg, None, hub.clock(), None)
    }
    frames = _frames(published, "notification")
    assert sorted(str(item.audience.id) for item in frames) == sorted([scene.admin.id, scene.reader.id])
    assert all(item.audience.kind == "user" and item.data["ref"] == ref for item in frames)
    hints = _frames(published, "ping")
    assert sorted(str(item.audience.id) for item in hints) == sorted([scene.device.id, scene.other_device.id])
    assert all(
        item.data == {"org_id": scene.org, "wg_id": scene.wg, "level": "workgroup", "entry_id": ref} for item in hints
    )
    audit = board.rows(
        "SELECT actor_id, org_id, wg_id, task_id, target_kind, target_id, detail, event_id FROM hub.audit"
        " WHERE action = 'whiteboard.notify'"
    )
    assert [(str(a), str(o), str(w), t, k, i, d, str(e)) for a, o, w, t, k, i, d, e in audit] == [
        (
            scene.writer.id,
            scene.org,
            scene.wg,
            None,
            "whiteboard_entry",
            ref,
            {"level": "workgroup", "recipients": 2},
            id_of(entry),
        )
    ]
    assert board.rows("SELECT notify FROM hub.whiteboard_entry WHERE id = %s", ref) == [(True,)]


def test_an_org_entry_notifies_every_member_and_pings_every_session(
    board: Board, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    _task, in_wg = _session(scene)
    _task, in_other = _session(scene, wg=board.other_wg)
    entry = board.posted(scene.admin, board.entry(scene.admin, level="org"), notify=[True])[0]
    signals.wait()
    ref = ref_of(entry)
    assert _recipients(board, ref) == {scene.writer.id, scene.reader.id, board.outsider.id}
    assert _pinged(board, ref) == {in_wg, in_other}
    assert [item.data for item in _frames(published, "ping")] == [
        {"org_id": scene.org, "level": "org", "entry_id": ref}
    ]
    rows = board.rows("SELECT DISTINCT wg_id, task_id FROM hub.notification WHERE ref = %s", ref)
    assert rows == [(None, None)]


def test_a_task_entry_pings_only_the_sessions_of_the_task(
    board: Board, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    task, session = _session(scene)
    _session(scene, device=scene.other_device)
    entry = board.posted(scene.writer, board.entry(scene.writer, level="task", task=task), notify=[True])[0]
    signals.wait()
    ref = ref_of(entry)
    assert _recipients(board, ref) == {scene.admin.id, scene.reader.id}
    assert _pinged(board, ref) == {session}
    assert [(str(item.audience.id), item.data) for item in _frames(published, "ping")] == [
        (scene.device.id, {"org_id": scene.org, "wg_id": scene.wg, "task_id": task, "level": "task", "entry_id": ref})
    ]
    assert board.rows("SELECT DISTINCT level FROM hub.session_ping WHERE ref = %s", ref) == [("task",)]


def test_without_notify_or_for_a_known_entry_nobody_is_notified(
    board: Board, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    _session(scene)
    quiet = board.posted(scene.writer, board.entry(scene.writer), notify=[False])[0]
    again = board.event(scene.writer, "whiteboard.entry", ref_of(quiet), wg=board.other_wg)
    board.posted(scene.writer, again, notify=[True])
    signals.wait()
    assert board.rows("SELECT count(*) FROM hub.notification") == [(0,)]
    assert board.rows("SELECT count(*) FROM hub.session_ping") == [(0,)]
    assert board.rows("SELECT count(*) FROM hub.audit WHERE action = 'whiteboard.notify'") == [(0,)]
    assert _frames(published, "notification") == []
    assert _frames(published, "ping") == []


def test_an_entry_without_readers_is_still_audited(board: Board, bound: None) -> None:
    lonely = board.scene.world.public_org()
    event = lonely.event(lonely.founder, "whiteboard.entry", ref=new_id())
    response = lonely.founder.browser.post("/v1/events", json={"events": [{"event": event, "notify": True}]})
    assert response.status_code == 200, response.text
    assert board.rows("SELECT detail FROM hub.audit WHERE action = 'whiteboard.notify'") == [
        ({"level": "org", "recipients": 0},)
    ]


def test_durable_pings_reach_the_device_state(board: Board, hub: HubHarness) -> None:
    scene = board.scene
    _task, session = _session(scene)
    _task, ended = _session(scene)
    entry = board.posted(scene.writer, board.entry(scene.writer), notify=[True])[0]  # no worker bound: no hint
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.session SET state = 'stopped' WHERE id = %s", (ended,))
    fleet = Fleet(scene.world)
    client = fleet.client(scene.device)
    state = client.get("/v1/device/state").json()
    [ping] = state["pings"]
    assert ping == {
        "ping_id": ping["ping_id"],
        "session_id": session,
        "kind": "whiteboard.entry",
        "level": "workgroup",
        "ref": ref_of(entry),
        "created_at": ping["created_at"],
    }
    other = fleet.client(scene.other_device).get("/v1/device/state").json()
    assert other["pings"] == []
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.session_ping SET delivered_at = %s", (hub.clock(),))
    assert client.get("/v1/device/state").json()["pings"] == []


def test_notifications_and_pings_hold_no_content(board: Board) -> None:
    """Spaltenprüfung: the two tables have exactly their content-free columns, and the stored values are IDs,
    closed kinds and times only."""
    scene = board.scene
    _session(scene)
    board.posted(scene.writer, board.entry(scene.writer), notify=[True])
    columns = {
        table: [
            row[0]
            for row in board.rows(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'hub' AND table_name = %s"
                " ORDER BY ordinal_position",
                table,
            )
        ]
        for table in ("notification", "session_ping")
    }
    assert columns == {
        "notification": ["id", "recipient_id", "kind", "org_id", "wg_id", "task_id", "ref", "created_at", "read_at"],
        "session_ping": ["id", "session_id", "kind", "level", "ref", "created_at", "delivered_at"],
    }
    texts = board.rows("SELECT kind, ref FROM hub.notification UNION ALL SELECT kind, ref FROM hub.session_ping")
    assert texts
    assert all(kind == "whiteboard.entry" and uuid.UUID(ref) for kind, ref in texts)


def test_a_task_of_another_workgroup_gets_no_entry_and_no_ping(
    board: Board, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    task, _id = _session(scene)  # a task of `wg`; the writer also writes `other_wg`
    foreign = board.entry(scene.writer, level="task", task=task, wg=board.other_wg)
    board.posted(scene.writer, foreign, notify=[True])
    signals.wait()
    ref = ref_of(foreign)
    assert board.rows("SELECT count(*) FROM hub.whiteboard_entry WHERE id = %s", ref) == [(0,)]
    assert _pinged(board, ref) == set()
    assert _recipients(board, ref) == set()
    assert _frames(published, "ping") == []


def test_a_repeated_entry_id_does_not_resend_the_original(
    board: Board, bound: None, published: publish.MemoryPublisher
) -> None:
    scene = board.scene
    _session(scene)
    original = board.posted(scene.admin, board.entry(scene.admin, level="org"), notify=[True])[0]
    signals.wait()
    before = len(published.items())
    again = board.event(scene.writer, "whiteboard.entry", ref_of(original), wg=board.other_wg)
    board.posted(scene.writer, again, notify=[True])
    signals.wait()
    assert len(published.items()) == before + 1  # only the event frame of the new event
    assert [item.frame for item in published.items()[before:]] == ["event"]


def test_pings_of_a_stopping_session_stay_listed(board: Board, hub: HubHarness) -> None:
    scene = board.scene
    _task, session = _session(scene)
    board.posted(scene.writer, board.entry(scene.writer), notify=[True])
    with hub.pool.connection() as connection, connection.transaction():
        connection.execute("UPDATE hub.session SET state = 'stopping' WHERE id = %s", (session,))
    pings = Fleet(scene.world).client(scene.device).get("/v1/device/state").json()["pings"]
    assert [ping["session_id"] for ping in pings] == [session]
