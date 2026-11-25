"""U-HUB-stream (platform browser, KOR-44): watching a session's browser on `GET /v1/stream` (`browser=`, rights,
audit `read.stream.browser`), the watch leases of each hub process with the device hint `browser`, the device route
`POST /v1/device/sessions/{session}/browser/live` (403/404/400/409/200) and the delivery of the latest frame to the
watching streams only, at once to a new viewer and across hub processes; `browser_action` frames reach only watching
streams and are not replayed (13-work-vm.md section 13.6, threat model B7-B12)."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from psycopg.types.json import Jsonb

from agent_hub.crypto import browserframe
from agent_hub.crypto.encoding import format_time
from agent_hub.device.access import device_view
from agent_hub.stream import browser
from agent_hub.stream.bus import BusMessage
from agent_hub.stream.publish import BROWSER_LIVE_REF, Audience, Published, publish
from tests.platform.hub.conftest import HubHarness, SigningKey
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.stream.support import (
    DeviceClient,
    LiveHub,
    Refused,
    SseClient,
    browser_key,
    device_key,
    free_port,
    live_hub,
    wait_for,
)

Json = dict[str, Any]
WK = bytes(range(32))
BROWSER = "br-0123456789ab"
JPEG = b"\xff\xd8 picture \xff\xd9"


def _frame(scene: Scene, session: str, seq: int = 0, **changes: Any) -> Json:
    fields: Json = {
        "browser": BROWSER,
        "session": session,
        "wg": scene.wg,
        "epoch": 1,
        "seq": seq,
        "ts": format_time(scene.world.hub.clock()),
    }
    fields.update(changes)
    return browserframe.seal_frame(WK, jpeg=JPEG, **fields)


def _post(
    hub: HubHarness, key: SigningKey, session: str, frame: Json | None = None, *, contract: bool = True
) -> httpx.Response:
    body = {} if frame is None else {"frame": frame}
    extensions = {} if contract else {"contract": False}
    return hub.client(key).post(f"/v1/device/sessions/{session}/browser/live", json=body, extensions=extensions)


def _leases(scene: Scene, session: str) -> list[tuple[Any, ...]]:
    return scene.rows(
        "SELECT instance, until, kbps FROM hub.browser_watch WHERE session_id = %s ORDER BY instance", session
    )


def _stored(scene: Scene, session: str) -> list[tuple[Any, ...]]:
    return scene.rows("SELECT browser_id, seq, frame FROM hub.browser_live WHERE session_id = %s", session)


def _device_rows(scene: Scene) -> int:
    return int(scene.rows("SELECT count(*) FROM hub.audit WHERE actor_id = %s", scene.device.id)[0][0])


def _hello(stream: SseClient) -> None:
    first = stream.frame()
    assert first is not None
    assert first.event == "hello"


def _watch(live: LiveHub, actor: Actor, org: str, session: str, **query: Any) -> SseClient:
    stream = live.stream(browser_key(actor), org=org, browser=session, **query)
    _hello(stream)
    return stream


def _run(live: LiveHub, work: Any) -> Any:
    loop = live.service.loop
    assert loop is not None
    return asyncio.run_coroutine_threadsafe(work, loop).result(30)


def _hint_of(device: DeviceClient) -> Json:
    while True:
        found = device.hint()
        if found["type"] == "browser":
            return found


def _outsider(scene: Scene) -> Actor:
    member = scene.world.user()
    scene.world.join(scene.draft, member)
    return member


def test_watch_rights_queries_and_audit(scene: Scene, live: LiveHub) -> None:
    task, session = scene.start_session()
    for actor, target in ((_outsider(scene), session), (scene.writer, new_id())):
        with pytest.raises(Refused) as refused:
            live.stream(browser_key(actor), org=scene.org, browser=target)
        assert (refused.value.status, refused.value.body) == (404, {"error": "not_found"})
    invalid: list[Json] = [
        {"browser": [new_id() for _ in range(4)]},
        {"browser": [session, session]},
        {"browser": session, "browser_kbps": "99"},
        {"browser": session, "browser_kbps": "10001"},
        {"browser": session, "browser_kbps": "fast"},
    ]
    for query in invalid:
        with pytest.raises(Refused) as refused:
            live.stream(browser_key(scene.writer), org=scene.org, **query)
        assert (refused.value.status, refused.value.body["error"]) == (400, "request_invalid"), query
    _watch(live, scene.writer, scene.org, session, terminal=session)
    rows = scene.rows(
        "SELECT action, result, org_id::text, wg_id::text, task_id::text, session_id::text FROM hub.audit"
        " WHERE action LIKE 'read.stream%%' AND result = 'ok' ORDER BY seq"
    )
    assert rows == [
        ("read.stream", "ok", scene.org, None, None, None),
        ("read.stream.terminal", "ok", scene.org, scene.wg, task, session),
        ("read.stream.browser", "ok", scene.org, scene.wg, task, session),
    ]


def test_leases_follow_the_local_streams_and_hint_the_device(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    _task, session = scene.start_session()
    device = live.device(device_key(hub.keys, scene.device, scene.org))
    assert device.hint()["type"] == "hello"
    instance = live.service.instance
    until = hub.clock() + timedelta(seconds=browser.LEASE_SECONDS)
    slow = _watch(live, scene.writer, scene.org, session, browser_kbps="250")
    assert _leases(scene, session) == [(instance, until, 250)]
    assert _hint_of(device) == {"type": "browser", "session_id": session}
    sharp = _watch(live, scene.reader, scene.org, session, browser_kbps="3000")
    assert _leases(scene, session) == [(instance, until, 3000)]
    assert _hint_of(device) == {"type": "browser", "session_id": session}
    sharp.close()
    assert wait_for(lambda: len(live.service.browser_watchers.get(uuid.UUID(session), ())) == 1)
    assert _leases(scene, session) == [(instance, until, 3000)]  # kept until the renewal
    hub.clock.advance(browser.RENEW_SECONDS)
    _run(live, live.service._sync({uuid.UUID(session)}, sweep=True))
    assert _leases(scene, session) == [(instance, until + timedelta(seconds=browser.RENEW_SECONDS), 250)]
    assert _hint_of(device) == {"type": "browser", "session_id": session}
    slow.close()
    assert wait_for(lambda: _leases(scene, session) == [])


def test_the_renewal_runs_every_period(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    _task, session = scene.start_session()
    device = live.device(device_key(hub.keys, scene.device, scene.org))
    assert device.hint()["type"] == "hello"
    _watch(live, scene.writer, scene.org, session)
    assert _hint_of(device)["session_id"] == session
    service = live.service

    def restart() -> None:
        assert service._renewer is not None
        service._renewer.cancel()
        service._renewer = asyncio.ensure_future(service._renew_loop())

    service.renew_seconds = 0.05
    assert service.loop is not None
    service.loop.call_soon_threadsafe(restart)
    hub.clock.advance(5)
    assert _hint_of(device)["session_id"] == session
    assert wait_for(lambda: _leases(scene, session)[0][1] == hub.clock() + timedelta(seconds=browser.LEASE_SECONDS))


def test_the_live_route(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    _task, session = scene.start_session()
    _other_task, foreign = scene.start_session(scene.other_device)
    key = device_key(hub.keys, scene.device, scene.org)
    assert _post(hub, key, new_id()).status_code == 404
    assert _post(hub, key, "not-a-session", contract=False).status_code == 404
    forbidden = _post(hub, key, foreign, _frame(scene, foreign))
    assert (forbidden.status_code, forbidden.json()) == (403, {"error": "forbidden"})
    for body in (_frame(scene, session), None):
        refused = _post(hub, key, session, body)
        assert (refused.status_code, refused.json()) == (409, {"error": "not_watched"})
    watching = _watch(live, scene.writer, scene.org, session)
    plain = live.stream(browser_key(scene.reader), org=scene.org)
    terminal = live.stream(browser_key(scene.reader), org=scene.org, terminal=session)
    _hello(plain)
    _hello(terminal)
    bad = [
        (_frame(scene, foreign), "/frame"),
        (_frame(scene, session, wg=new_id()), "/frame"),
        ({**_frame(scene, session), "v": 2}, "/frame"),
        (_frame(scene, session, epoch=7), "/frame/epoch"),
    ]
    for frame, pointer in bad:
        invalid = _post(hub, key, session, frame, contract=False)
        assert invalid.status_code == 400, frame
        assert invalid.json() == {"error": "request_invalid", "detail": {"pointers": [pointer]}}
    until = format_time(hub.clock() + timedelta(seconds=browser.LEASE_SECONDS))
    empty = _post(hub, key, session)
    assert (empty.status_code, empty.json()) == (200, {"until": until, "kbps": 1000})
    assert _stored(scene, session) == []
    first = _frame(scene, session, seq=1)
    assert _post(hub, key, session, first).json() == {"until": until, "kbps": 1000}
    assert _stored(scene, session) == [(BROWSER, 1, first)]
    shown = watching.frame()
    assert shown is not None
    assert (shown.event, shown.data) == ("browser_live", {"session_id": session, "frame": first})
    second = _frame(scene, session, seq=2)
    audited = _device_rows(scene)
    assert _post(hub, key, session, second).status_code == 200
    assert _stored(scene, session) == [(BROWSER, 2, second)]  # the latest frame replaces the one before
    assert watching.frame().data["frame"] == second  # type: ignore[union-attr]
    assert plain.quiet() == []
    assert terminal.quiet() == []
    older = _frame(scene, session, seq=1)
    assert _post(hub, key, session, older).json() == {"until": until, "kbps": 1000}  # answered, not taken
    assert _stored(scene, session) == [(BROWSER, 2, second)]
    assert watching.quiet() == []
    viewer = live.stream(browser_key(scene.reader), org=scene.org, browser=session)
    hello, initial = viewer.frames(2)
    assert hello.event == "hello"
    assert (initial.event, initial.data) == ("browser_live", {"session_id": session, "frame": second})
    assert _device_rows(scene) == audited  # no audit row per frame (the generic row is windowed)
    for stream in (watching, viewer):
        stream.close()
    assert wait_for(lambda: _leases(scene, session) == [] and _stored(scene, session) == [])


def test_browser_actions_reach_only_watching_streams_and_are_not_replayed(scene: Scene, live: LiveHub) -> None:
    _task, session = scene.start_session()
    watching = _watch(live, scene.writer, scene.org, session)
    plain = live.stream(browser_key(scene.reader), org=scene.org)
    _hello(plain)
    [action] = scene.append(scene.device, scene.session_event(scene.device, session, "browser.action"))
    [state] = scene.append(scene.device, scene.session_event(scene.device, session, "browser.state"))
    first, second = watching.frames(2)
    assert (first.event, first.data["scope_seq"]) == ("browser_action", action.scope_seq)
    assert (second.event, second.data["scope_seq"]) == ("event", state.scope_seq)
    gap = plain.frame()
    assert gap is not None
    assert (gap.event, gap.data["scope_seq"]) == ("event", state.scope_seq)
    cursor = f"{scene.wg}:{action.scope_seq - 1}"
    again = live.stream(browser_key(scene.writer), org=scene.org, browser=session, cursors=cursor)
    assert [(item.event, item.data.get("scope_seq")) for item in again.frames(2)][1] == ("event", state.scope_seq)
    assert again.quiet() == []


def test_frames_and_leases_across_hub_processes(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    _task, session = scene.start_session()
    key = device_key(hub.keys, scene.device, scene.org)
    with live_hub(hub, free_port(15)) as other:
        here = _watch(live, scene.writer, scene.org, session, browser_kbps="250")
        there = _watch(other, scene.reader, scene.org, session, browser_kbps="3000")
        assert sorted(row[0] for row in _leases(scene, session)) == sorted(
            [live.service.instance, other.service.instance]
        )
        assert _post(hub, key, session).json()["kbps"] == 3000  # the largest wish among the valid leases
        frame = _frame(scene, session, seq=5)
        with scene.world.hub.pool.connection() as connection:
            connection.execute(
                "INSERT INTO hub.browser_live (session_id, browser_id, seq, frame, received_at)"
                " VALUES (%s, %s, 5, %s, now())",
                (session, BROWSER, Jsonb(frame)),
            )
        reference = Published(
            Audience("org", uuid.UUID(scene.org)),
            BROWSER_LIVE_REF,
            {"session_id": session, "seq": 5},
            wg_id=uuid.UUID(scene.wg),
        )
        live.service.publish(reference)  # here at once, there through the bus
        assert here.frame().data["frame"] == frame  # type: ignore[union-attr]
        assert there.frame().data["frame"] == frame  # type: ignore[union-attr]
        other.service._on_message(BusMessage("again", reference))  # the same frame once more
        assert there.quiet() == []
        there.close()
        assert wait_for(lambda: [row[0] for row in _leases(scene, session)] == [live.service.instance])
        assert _stored(scene, session) != []  # a valid lease remains: the frame stays
    with live_hub(hub, free_port(16)) as third:
        _watch(third, scene.writer, scene.org, session)
        assert len(_leases(scene, session)) == 2
    assert [row[0] for row in _leases(scene, session)] == [live.service.instance]  # a stopped hub drops its leases
    here.close()
    assert wait_for(lambda: _leases(scene, session) == [] and _stored(scene, session) == [])


def test_lost_rights_end_the_watch(scene: Scene, live: LiveHub) -> None:
    _task, session = scene.start_session()
    stream = _watch(live, scene.reader, scene.org, session)
    assert _leases(scene, session) != []
    with scene.world.hub.pool.connection() as connection:
        connection.execute(
            "UPDATE hub.wg_access SET revoked_seq = 999 WHERE wg_id = %s AND user_id = %s", (scene.wg, scene.reader.id)
        )
    seq, digest = scene.rows("SELECT log_head_seq, log_head_hash FROM hub.org WHERE id = %s", scene.org)[0]
    publish(Audience("org", uuid.UUID(scene.org)), "log", {"org_id": scene.org, "head": {"seq": seq, "hash": digest}})
    assert stream.frame().event == "log"  # type: ignore[union-attr]
    assert wait_for(lambda: _leases(scene, session) == [])
    assert live.service.browser_watchers == {}


def test_the_renewal_sweeps_ended_leases_of_other_processes(scene: Scene, hub: HubHarness, live: LiveHub) -> None:
    _task, session = scene.start_session()
    now = hub.clock()
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.browser_watch (session_id, instance, until, kbps) VALUES (%s, 'gone', %s, 1000)",
            (session, now),
        )
        connection.execute(
            "INSERT INTO hub.browser_live (session_id, browser_id, seq, frame, received_at) VALUES (%s, %s, 0, %s, %s)",
            (session, BROWSER, Jsonb(_frame(scene, session)), now),
        )
    _run(live, live.service._sync(set(), sweep=True))
    assert (_leases(scene, session), _stored(scene, session)) == ([], [])


def test_failing_lease_writes_are_logged(
    scene: Scene, hub: HubHarness, live: LiveHub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _task, session = scene.start_session()

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(browser, "write_leases", broken)
    stream = _watch(live, scene.writer, scene.org, session)  # the stream opens; the renewal tries again
    assert "watch leases could not be written" in caplog.text
    assert _leases(scene, session) == []
    stream.close()
    monkeypatch.setattr(browser, "release_instance", broken)
    with live_hub(hub, free_port(16)):
        pass
    assert wait_for(lambda: "could not be deleted" in caplog.text)


def test_a_device_key_is_needed(scene: Scene, hub: HubHarness) -> None:
    _task, session = scene.start_session()
    refused = hub.client(cast(SigningKey, scene.writer.browser.key)).post(
        f"/v1/device/sessions/{session}/browser/live", json={}
    )
    assert refused.status_code == 403


def test_a_new_browser_session_restarts_the_frames_and_posts_are_capped(
    scene: Scene, hub: HubHarness, live: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _task, session = scene.start_session()
    key = device_key(hub.keys, scene.device, scene.org)
    watching = _watch(live, scene.writer, scene.org, session)
    assert _post(hub, key, session, _frame(scene, session, seq=9)).status_code == 200
    assert watching.frame().data["frame"]["seq"] == 9  # type: ignore[union-attr]
    restarted = _frame(scene, session, seq=0, browser="br-ba9876543210")
    assert _post(hub, key, session, restarted).status_code == 200
    assert _stored(scene, session) == [("br-ba9876543210", 0, restarted)]
    assert watching.frame().data["frame"] == restarted  # type: ignore[union-attr]
    monkeypatch.setattr(browser, "LIMITER", browser.FrameLimiter(limit=1))
    assert _post(hub, key, session, _frame(scene, session, seq=1, browser="br-ba9876543210")).status_code == 200
    capped = _post(hub, key, session, _frame(scene, session, seq=2, browser="br-ba9876543210"))
    assert (capped.status_code, capped.json(), capped.headers["retry-after"]) == (429, {"error": "rate_limited"}, "1")
    assert _stored(scene, session)[0][1] == 1  # nothing stored
    assert _post(hub, key, session).status_code == 200  # an empty post only answers the lease: not counted


def test_the_frame_limiter() -> None:
    now = [0.0]
    limiter = browser.FrameLimiter(limit=2, clock=lambda: now[0])
    first, second = uuid.uuid4(), uuid.uuid4()
    limiter.admit(first)
    limiter.admit(first)
    limiter.admit(second)
    with pytest.raises(Exception, match="rate_limited"):
        limiter.admit(first)
    now[0] = 0.5
    with pytest.raises(Exception, match="rate_limited"):
        limiter.admit(first)
    now[0] = 1.0
    limiter.admit(first)  # the posts of the first second have left the window
    assert set(limiter._posts) == {first}  # a session without a post in the last second is forgotten
    now[0] = 1.5
    limiter.admit(first)
    now[0] = 2.2
    limiter.admit(first)  # the post at 1.0 left the window, the one at 1.5 still counts
    with pytest.raises(Exception, match="rate_limited"):
        limiter.admit(first)


def test_a_frame_is_never_left_without_a_lease(scene: Scene, hub: HubHarness) -> None:
    _task, session = scene.start_session()
    now = hub.clock()
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.browser_watch (session_id, instance, until, kbps) VALUES (%s, 'other', %s, 500)",
            (session, now + timedelta(seconds=browser.LEASE_SECONDS)),
        )
    released = threading.Event()

    def release() -> None:  # the other process's last stream stops watching the session
        with hub.pool.connection() as connection, connection.transaction():
            browser.write_leases(connection, "other", {uuid.UUID(session): None}, now)
        released.set()

    device = uuid.UUID(scene.device.id)
    with hub.pool.connection() as connection, connection.transaction():
        view = device_view(connection, device, hub.config.platform_root_key)
        frame = _frame(scene, session, seq=3)
        posted = browser.post_live(connection, view, device, uuid.UUID(session), frame=frame, now=now)
        assert (posted.answer["kbps"], posted.seq) == (500, 3)
        worker = threading.Thread(target=release)
        worker.start()
        time.sleep(0.3)
        assert not released.is_set()  # the lease is held until the frame is committed
    worker.join(10)
    assert released.is_set()
    assert (_leases(scene, session), _stored(scene, session)) == ([], [])
