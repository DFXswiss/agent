"""U-HUB-stream (browsers): `GET /v1/stream` on a real uvicorn server with a streaming client - hello and heads,
live frames, heartbeat comments, cursor replay, the gap trigger, resync after a replay past the limit, a buffer
overflow and a hub restart, and the rights filter per frame including watched terminals (03-api.md section 5,
ADR-016). Every frame is validated against contracts/ws/sse/."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from agent_hub.stream import sse
from agent_hub.stream.publish import Audience, Published, publish
from tests.platform.hub.conftest import HubHarness
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.stream.support import LiveHub, Refused, free_port, live_hub, signed_headers, wait_for

Json = dict[str, Any]


def _key(actor: Any) -> Any:
    return actor.browser.key


def _head(scene: Scene, scope: str) -> int:
    rows = scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scope)
    return int(rows[0][0]) if rows else 0


def _post_wg(scene: Scene, count: int = 1) -> list[int]:
    seqs = []
    for _ in range(count):
        response = scene.post(scene.writer, scene.wg_event(scene.writer))
        assert response.status_code == 200, response.text
        seqs.append(response.json()["results"][0]["scope_seq"])
    return seqs


def _outsider(scene: Scene) -> Any:
    """A member of the org without access to the scene's workgroup (only the default workgroup)."""
    member = scene.world.user()
    scene.world.join(scene.draft, member)
    return member


def _log_frame(scene: Scene) -> Json:
    seq, digest = scene.rows("SELECT log_head_seq, log_head_hash FROM hub.org WHERE id = %s", scene.org)[0]
    return {"org_id": scene.org, "head": {"seq": seq, "hash": digest}}


def _session_row(scene: Scene, session: str) -> Json:
    task, org, wg, owner, device = scene.rows(
        "SELECT task_id::text, org_id::text, wg_id::text, owner_id::text, device_id::text FROM hub.session"
        " WHERE id = %s",
        session,
    )[0]
    now = "2026-11-21T12:00:00.123Z"
    return {
        "session_id": session,
        "task_id": task,
        "org_id": org,
        "wg_id": wg,
        "owner_id": owner,
        "device_id": device,
        "vm_id": None,
        "parent_session_id": None,
        "origin_operation_id": None,
        "depth": 0,
        "state": "working",
        "state_reason": None,
        "model_access": None,
        "model_id": None,
        "agent_kind": "claude-code",
        "agent_version": None,
        "estimate_ms": None,
        "created_at": now,
        "ready_at": None,
        "first_working_at": None,
        "ended_at": None,
    }


def _operation_row(scene: Scene, task: str, session: str) -> Json:
    now = "2026-11-21T12:00:00.123Z"
    return {
        "operation_id": new_id(),
        "org_id": scene.org,
        "wg_id": scene.wg,
        "task_id": task,
        "session_id": session,
        "device_id": scene.device.id,
        "actor_id": scene.writer.id,
        "type": "task.set_title",
        "state": "accepted",
        "error_code": None,
        "retryable": False,
        "duplicate_of": None,
        "attempt": 1,
        "params_event_id": None,
        "result_event_id": None,
        "created_at": now,
        "decided_at": now,
        "started_at": None,
        "finished_at": None,
    }


def _task_row(scene: Scene, wg: str) -> Json:
    now = "2026-11-21T12:00:00.123Z"
    return {
        "task_id": new_id(),
        "org_id": scene.org,
        "wg_id": wg,
        "owner_id": scene.writer.id,
        "state": "working",
        "state_reason": None,
        "created_at": now,
        "state_at": now,
        "last_activity_at": now,
        "closed_at": None,
        "main_session_id": None,
        "title_event_id": None,
    }


def test_hello_heads_then_live_events(scene: Scene, live: LiveHub) -> None:
    stream = live.stream(_key(scene.writer), org=scene.org)
    hello = stream.frame()
    assert hello is not None
    assert hello.event == "hello"
    default_wg = str(scene.rows("SELECT default_wg FROM hub.org WHERE id = %s", scene.org)[0][0])
    expected = {scope: _head(scene, scope) for scope in (scene.org, scene.wg, default_wg)}
    assert hello.data["heads"] == expected
    assert hello.data["hub_time"] == "2026-11-21T12:00:00.123Z"
    uuid.UUID(hello.data["stream_id"])
    [seq] = _post_wg(scene)
    frame = stream.frame()
    assert frame is not None
    assert (frame.event, frame.data["scope_id"], frame.data["scope_seq"]) == ("event", scene.wg, seq)
    assert stream.response.headers["content-type"].startswith("text/event-stream")
    assert stream.response.headers["cache-control"] == "no-store"
    assert "agent-hub-signature" not in stream.response.headers


def test_heartbeat_comment(scene: Scene, live: LiveHub) -> None:
    live.service.heartbeat_seconds = 0.2
    stream = live.stream(_key(scene.reader), org=scene.org)
    assert stream.frame().event == "hello"  # type: ignore[union-attr]
    beat = stream.next(2)
    assert beat is not None
    assert beat.event == ":"


def test_cursor_replay_and_no_duplicates(scene: Scene, live: LiveHub) -> None:
    start = _head(scene, scene.wg)
    seqs = _post_wg(scene, 3)
    org_head = _head(scene, scene.org)
    stream = live.stream(_key(scene.reader), org=scene.org, cursors=f"{scene.wg}:{start},{scene.org}:{org_head}")
    frames = stream.frames(4)
    assert frames[0].event == "hello"
    assert [(item.event, item.data["scope_seq"]) for item in frames[1:]] == [("event", seq) for seq in seqs]
    [later] = _post_wg(scene)
    assert stream.frame().data["scope_seq"] == later  # type: ignore[union-attr]
    assert stream.quiet() == []


def test_replay_past_the_limit_sends_resync(scene: Scene, live: LiveHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sse, "REPLAY_LIMIT", 2)
    start = _head(scene, scene.wg)
    _post_wg(scene, 3)
    stream = live.stream(_key(scene.reader), org=scene.org, cursors=f"{scene.wg}:{start}")
    hello, resync = stream.frames(2)
    assert hello.event == "hello"
    assert (resync.event, resync.data) == ("resync", {"reason": "replay_limit"})


def test_unwatched_terminal_leaves_a_gap_and_watched_terminal_is_sent(scene: Scene, live: LiveHub) -> None:
    _, session = scene.start_session()
    watching = live.stream(_key(scene.writer), org=scene.org, terminal=session)
    plain = live.stream(_key(scene.reader), org=scene.org)
    assert watching.frame().event == "hello"  # type: ignore[union-attr]
    assert plain.frame().event == "hello"  # type: ignore[union-attr]
    [terminal] = scene.append(scene.device, scene.session_event(scene.device, session, "session.terminal"))
    [output] = scene.append(scene.device, scene.session_event(scene.device, session, "session.output"))
    first = watching.frames(2)
    assert [(item.event, item.data["scope_seq"]) for item in first] == [
        ("terminal", terminal.scope_seq),
        ("event", output.scope_seq),
    ]
    gap = plain.frame()
    assert gap is not None
    # the reader's cursor is below the terminal event: the frame above cursor + 1 makes the client pull the gap
    assert (gap.event, gap.data["scope_seq"]) == ("event", terminal.scope_seq + 1)
    assert plain.quiet() == []
    # a replay includes the watched session's terminal events only
    cursor = f"{scene.wg}:{terminal.scope_seq - 1}"
    replay = live.stream(_key(scene.writer), org=scene.org, cursors=cursor, terminal=session)
    assert [item.event for item in replay.frames(3)] == ["hello", "terminal", "event"]
    unwatched = live.stream(_key(scene.reader), org=scene.org, cursors=cursor)
    assert [item.event for item in unwatched.frames(2)] == ["hello", "event"]


def test_rights_filter_per_frame(scene: Scene, live: LiveHub) -> None:
    admin = live.stream(_key(_outsider(scene)), org=scene.org)  # a member without access to the workgroup
    reader = live.stream(_key(scene.reader), org=scene.org, monitoring="1")
    hello = admin.frame()
    assert hello is not None
    assert scene.wg not in hello.data["heads"]
    assert reader.frame().event == "hello"  # type: ignore[union-attr]
    _post_wg(scene)
    assert reader.frame().event == "event"  # type: ignore[union-attr]
    task = _task_row(scene, scene.wg)
    publish(Audience("org", uuid.UUID(scene.org)), "task", task)
    assert reader.frame().data == task  # type: ignore[union-attr]
    publish(Audience("org", uuid.UUID(scene.org)), "metrics", {"samples": []}, wg_id=uuid.UUID(scene.wg))
    assert reader.frame().event == "metrics"  # type: ignore[union-attr]
    publish(Audience("org", uuid.UUID(scene.org)), "metrics", {"samples": []})
    assert reader.frame().event == "metrics"  # type: ignore[union-attr]
    publish(Audience("user", uuid.UUID(scene.reader.id)), "wraps", {})
    assert reader.frame().event == "wraps"  # type: ignore[union-attr]
    org_frame = scene.post(scene.admin, scene.org_event(scene.admin))
    assert org_frame.status_code == 200
    assert reader.frame().data["scope_id"] == scene.org  # type: ignore[union-attr]
    # the admin saw neither the workgroup event, the workgroup's task nor the metrics (no monitoring flag) nor wraps
    seen = admin.frame()  # the org event must come; then nothing else (a 0.5 s window alone was a deadline)
    assert seen is not None
    assert (seen.event, seen.data.get("scope_id")) == ("event", scene.org)
    assert admin.quiet() == []


def test_session_and_operation_frames_follow_the_workgroup(scene: Scene, live: LiveHub) -> None:
    outsider = live.stream(_key(_outsider(scene)), org=scene.org)
    reader = live.stream(_key(scene.reader), org=scene.org)
    assert outsider.frame().event == "hello"  # type: ignore[union-attr]
    assert reader.frame().event == "hello"  # type: ignore[union-attr]
    task, session = scene.start_session()
    org = Audience("org", uuid.UUID(scene.org))
    publish(org, "session", {"session": _session_row(scene, session), "transition": None})
    publish(org, "operation", _operation_row(scene, task, session))
    publish(org, "log", _log_frame(scene))
    assert [item.event for item in reader.frames(3)] == ["session", "operation", "log"]
    assert outsider.frame().event == "log"  # type: ignore[union-attr]
    assert outsider.quiet() == []


def test_refused_requests(scene: Scene, live: LiveHub, hub: HubHarness) -> None:
    with pytest.raises(Refused) as stranger:
        live.stream(_key(scene.stranger), org=scene.org)
    assert (stranger.value.status, stranger.value.body) == (404, {"error": "not_found"})
    _, session = scene.start_session()
    with pytest.raises(Refused) as foreign_terminal:
        live.stream(_key(_outsider(scene)), org=scene.org, terminal=session)
    assert foreign_terminal.value.status == 404
    with pytest.raises(Refused) as unknown_terminal:
        live.stream(_key(scene.writer), org=scene.org, terminal=new_id())
    assert unknown_terminal.value.status == 404
    invalid: list[Json] = [
        {"org": "nope"},
        {"org": scene.org, "terminal": [new_id() for _ in range(4)]},
        {"org": scene.org, "terminal": [session, session]},
        {"org": scene.org, "cursors": f"{scene.wg}:x"},
        {"org": scene.org, "cursors": f"{scene.wg}:1,{scene.wg}:2"},
        {"org": scene.org, "cursors": ",".join(f"{new_id()}:1" for _ in range(257))},
        {"org": scene.org, "monitoring": "2"},
        {"org": scene.org, "other": "1"},
        {},
    ]
    for query in invalid:
        with pytest.raises(Refused) as refused:
            live.stream(_key(scene.writer), **query)
        assert (refused.value.status, refused.value.body["error"]) == (400, "request_invalid"), query
    device = hub.keys.device()
    with pytest.raises(Refused) as wrong_kind:
        live.stream(device, org=scene.org)
    assert (wrong_kind.value.status, wrong_kind.value.body) == (403, {"error": "forbidden"})
    unsigned = httpx.get(f"{live.base}/v1/stream?org={scene.org}", timeout=5)
    assert (unsigned.status_code, unsigned.json()) == (401, {"error": "signature_missing"})


def test_cursors_of_unreadable_scopes_are_ignored(scene: Scene, live: LiveHub) -> None:
    _post_wg(scene, 2)
    stream = live.stream(_key(_outsider(scene)), org=scene.org, cursors=f"{scene.wg}:0,{new_id()}:0")
    assert stream.frame().event == "hello"  # type: ignore[union-attr]
    assert stream.quiet() == []


def test_buffer_overflow_sends_resync(scene: Scene, live: LiveHub) -> None:
    live.service.buffer = 3
    stream = live.stream(_key(scene.reader), org=scene.org)
    assert stream.frame().event == "hello"  # type: ignore[union-attr]
    org = uuid.UUID(scene.org)
    user = uuid.UUID(scene.reader.id)

    def burst() -> None:
        for _ in range(5):
            live.service._deliver(sse_item(org, user))

    live.service.loop.call_soon_threadsafe(burst)  # type: ignore[union-attr]
    frames = stream.frames(3)
    assert [item.event for item in frames] == ["resync", "wraps", "wraps"]
    assert frames[0].data == {"reason": "buffer_overflow"}


def sse_item(org: uuid.UUID, user: uuid.UUID) -> Any:
    return Published(Audience("user", user), "wraps", {})


def test_rights_are_read_again_on_log_frames_and_heartbeats(scene: Scene, live: LiveHub) -> None:
    live.service.heartbeat_seconds = 0.2
    stream = live.stream(_key(scene.reader), org=scene.org)
    assert stream.frame().event == "hello"  # type: ignore[union-attr]
    with scene.world.hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.wg_access SET revoked_seq = 999 WHERE wg_id = %s AND user_id = %s", (scene.wg, scene.reader.id)
        )
    publish(Audience("org", uuid.UUID(scene.org)), "log", _log_frame(scene))
    assert stream.frame().event == "log"  # type: ignore[union-attr]
    assert wait_for(
        lambda: all(uuid.UUID(scene.wg) not in item.scopes for item in live.service.by_org[uuid.UUID(scene.org)])
    )
    _post_wg(scene)
    assert stream.quiet() == []
    # leaving the org ends the stream at the next heartbeat
    with scene.world.hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.org_member SET removed_seq = 999 WHERE org_id = %s AND principal_id = %s",
            (scene.org, scene.reader.id),
        )
    assert stream.ended(3)


def test_refresh_keeps_the_rights_when_the_database_fails(
    scene: Scene, live: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.service.heartbeat_seconds = 0.1
    stream = live.stream(_key(scene.reader), org=scene.org)
    assert stream.frame().event == "hello"  # type: ignore[union-attr]

    def broken(*_args: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(sse, "readable_scopes", broken)
    monkeypatch.setattr("agent_hub.stream.service.readable_scopes", broken)
    assert stream.next(2).event == ":"  # type: ignore[union-attr]
    assert stream.next(2).event == ":"  # type: ignore[union-attr]
    monkeypatch.undo()
    _post_wg(scene)
    assert stream.frame().event == "event"  # type: ignore[union-attr]


def test_hub_restart_resyncs_and_the_replay_continues(scene: Scene, hub: HubHarness) -> None:
    port = free_port(15)
    with live_hub(hub, port) as first:
        stream = first.stream(_key(scene.reader), org=scene.org)
        hello = stream.frame()
        assert hello is not None
        cursor = hello.data["heads"][scene.wg]
        first.server.should_exit = True
        resync = stream.frame()
        assert resync is not None
        assert (resync.event, resync.data) == ("resync", {"reason": "hub_restart"})
        assert stream.ended()
        first.thread.join(10)
        assert not first.thread.is_alive()
    seqs = _post_wg(scene, 2)
    with live_hub(hub, port) as second:
        again = second.stream(_key(scene.reader), org=scene.org, cursors=f"{scene.wg}:{cursor}")
        frames = again.frames(3)
        assert [item.data.get("scope_seq") for item in frames[1:]] == seqs


def test_a_closing_hub_refuses_new_streams(scene: Scene, live: LiveHub) -> None:
    live.service.closing = True
    with pytest.raises(Refused) as refused:
        live.stream(_key(scene.reader), org=scene.org)
    assert (refused.value.status, refused.value.body) == (503, {"error": "unavailable"})


def test_query_parser() -> None:
    org, wg = new_id(), new_id()
    query = sse.parse_query(org, [], f"{wg}:0,{org}:17", "1")
    assert query.cursors == {uuid.UUID(wg): 0, uuid.UUID(org): 17}
    assert query.monitoring
    assert sse.parse_query(org, [], None, None).cursors == {}


def test_signed_headers_cover_the_target(live: LiveHub, hub: HubHarness) -> None:
    headers = signed_headers(hub.keys.session(), live.clock, "/v1/stream?org=x")
    assert set(headers) == {"Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature"}
