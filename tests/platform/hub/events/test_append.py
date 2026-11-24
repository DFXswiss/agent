"""U-HUB-events (append): `POST /v1/events` and `append_events` - E1-E9 at the hub, the grace period of E6 with the
fake clock, idempotency and conflicts, batch atomicity, the stored columns, the audit row, frames, hints and the
signal `event.appended` (03-api.md section 4.4, 04-crypto.md sections 4.3, 5.12, 6.4, 02-data-model.md 2.3)."""

from __future__ import annotations

import copy
import secrets
import uuid
from typing import Any

import pytest

from agent_hub.crypto import event as event_format
from agent_hub.crypto import signed
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, canonical, format_time
from agent_hub.errors import HubError
from agent_hub.events.notify import announce
from agent_hub.events.store import SESSION_KINDS, append_events
from agent_hub.stream.publish import Audience, MemoryPublisher
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id

Json = dict[str, Any]


def _error(response: Any) -> tuple[int, Json]:
    return response.status_code, response.json()


def _rotate_wg(scene: Scene) -> None:
    """Rotate the workgroup to epoch 2 (receipt time = the hub clock now)."""
    draft, admin = scene.draft, scene.admin
    draft.add("rotate", admin, scope="wg", scope_id=scene.wg, epoch=2)
    readers = [admin, scene.writer, scene.reader, scene.device, scene.other_device]
    response = draft.post(admin, draft.wraps(admin, "wg", scene.wg, 2, readers))
    assert response.status_code == 200, response.text


def test_post_stores_every_event_with_its_columns(scene: Scene, published: MemoryPublisher, emitted: list[Any]) -> None:
    task = new_id()
    profile = scene.org_event(scene.admin)
    prompt = scene.prompt(scene.writer, task)
    entry = new_id()
    board = scene.wg_event(scene.writer, "whiteboard.entry", ref=entry)
    response = scene.post(scene.writer, prompt, board, notify=[False, True])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "results": [
            {"id": prompt["header"]["id"], "scope_id": scene.wg, "scope_seq": 1, "replayed": False},
            {"id": board["header"]["id"], "scope_id": scene.wg, "scope_seq": 2, "replayed": False},
        ]
    }
    assert scene.post(scene.admin, profile).json()["results"][0]["scope_seq"] == 4  # 3 genesis profiles
    row = scene.rows(
        "SELECT scope, org_id::text, wg_id::text, task_id::text, session_id, author_id::text, author_kind, kind, ref,"
        " epoch, ts, lh, header_text, ct, sig, size, origin_device_id, origin_seq, received_at"
        " FROM hub.event WHERE id = %s",
        prompt["header"]["id"],
    )[0]
    header = prompt["header"]
    assert row[:10] == ("wg", scene.org, scene.wg, task, None, scene.writer.id, "user", "prompt", "", 1)
    assert format_time(row[10]) == header["ts"]
    assert row[11] == header["lh"]
    assert row[12] == canonical(header).decode("ascii")
    assert (bytes(row[13]), bytes(row[14])) == (b64u_decode(prompt["ct"]), b64u_decode(prompt["sig"]))
    assert row[15] == len(b64u_decode(prompt["ct"]))
    assert (row[16], row[17], row[18]) == (None, None, scene.world.hub.clock())
    assert scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg) == [(2,)]
    audit = scene.rows(
        "SELECT actor_kind, actor_id::text, result, org_id::text, wg_id::text, task_id::text, target_kind,"
        " target_id, detail, event_id::text FROM hub.audit WHERE action = 'event.append' AND event_id = %s",
        prompt["header"]["id"],
    )
    ct_bytes = len(b64u_decode(prompt["ct"]))
    assert audit == [
        (
            "user",
            scene.writer.id,
            "ok",
            scene.org,
            scene.wg,
            task,
            "event",
            prompt["header"]["id"],
            {"kind": "prompt", "scope": "wg", "scope_seq": 1, "ct_bytes": ct_bytes},
            prompt["header"]["id"],
        )
    ]
    frames = [(item.audience, item.frame, dict(item.data)) for item in published.items()]
    org = Audience("org", uuid.UUID(scene.org))
    assert (org, "event", {"scope_id": scene.wg, "scope_seq": 1, "event": prompt}) in frames
    hints = sorted((item.audience.id, item.data["scope_seq"]) for item in published.items() if item.frame == "events")
    devices = sorted([uuid.UUID(scene.device.id), uuid.UUID(scene.other_device.id)])
    # wg events reach both assigned devices; the org profile reaches them too (they read the org scope)
    assert hints == sorted([(d, 1) for d in devices] + [(d, 2) for d in devices] + [(d, 4) for d in devices])
    assert [ids["notify"] for _name, ids in emitted] == [0, 1, 0]
    first = emitted[0][1]
    assert first == {
        "event_id": uuid.UUID(prompt["header"]["id"]),
        "org_id": uuid.UUID(scene.org),
        "scope": "wg",
        "scope_id": uuid.UUID(scene.wg),
        "scope_seq": 1,
        "kind": "prompt",
        "author_id": uuid.UUID(scene.writer.id),
        "notify": 0,
        "wg_id": uuid.UUID(scene.wg),
        "task_id": uuid.UUID(task),
    }
    assert emitted[1][1]["ref"] == entry


def test_device_events_with_origin_and_hints_skip_the_author(scene: Scene, published: MemoryPublisher) -> None:
    task, session = scene.start_session()
    event = scene.session_event(scene.device, session)
    params = scene.wg_event(scene.device, "operation.params", ref=new_id())
    stored, second = scene.append(scene.device, event, params, origin_seqs=[7, 8])
    assert second.scope_seq == 2
    assert (stored.scope_seq, stored.task_id, stored.session_id) == (1, task, session)
    assert stored.devices == frozenset({scene.other_device.id})
    row = scene.rows("SELECT author_kind, origin_device_id::text, origin_seq FROM hub.event WHERE id = %s", stored.id)
    assert row == [("device", scene.device.id, 7)]
    audit = "SELECT actor_kind, device_id::text, action FROM hub.audit WHERE event_id = %s"
    # the session's own output gets no row of its own: the audit job summarises it (ADR-063 section 4)
    assert scene.rows(audit, stored.id) == []
    assert scene.rows(audit, second.id) == [("device", scene.device.id, "event.append")]
    with pytest.raises(ValueError, match="one origin_seq"):
        scene.append(scene.device, event, origin_seqs=[9, 10])


def test_terminal_events_go_out_as_terminal_frames(scene: Scene, published: MemoryPublisher) -> None:
    _task, session = scene.start_session()
    stored = scene.append(scene.device, scene.session_event(scene.device, session, "session.terminal"))
    announce(stored)
    assert [item.frame for item in published.items()] == ["terminal"]  # no device hint: pulls skip terminal


def test_notify_only_on_whiteboard_entries(scene: Scene) -> None:
    settings = scene.wg_event(scene.writer)
    assert _error(scene.post(scene.writer, settings, notify=[True])) == (400, {"error": "request_invalid"})
    edit = scene.wg_event(scene.writer, "whiteboard.edit", ref=new_id())
    assert _error(scene.post(scene.writer, edit, notify=[True])) == (400, {"error": "request_invalid"})
    assert scene.post(scene.writer, settings, notify=[False]).status_code == 200
    hub = scene.world.hub
    with hub.pool.connection() as connection, pytest.raises(ValueError, match="one flag"):
        append_events(connection, [settings], scene.writer.id, now=hub.clock(), root_key=b"", notify=[True, False])


# --- E1-E8 ------------------------------------------------------------------------------------------------------


def test_e1_e2_header_format(scene: Scene) -> None:
    extra = scene.org_event(scene.admin)
    extra["header"]["seq"] = 1
    assert _error(scene.post(scene.admin, extra, extensions={"contract": False})) == (
        400,
        {"error": "header_invalid"},
    )
    version = scene.org_event(scene.admin)
    version["header"]["v"] = 2
    assert _error(scene.post(scene.admin, version, extensions={"contract": False}))[1] == {"error": "header_invalid"}
    alg = scene.org_event(scene.admin)
    alg["header"]["alg"] = "A128GCM"
    assert _error(scene.post(scene.admin, alg, extensions={"contract": False}))[1] == {"error": "header_invalid"}
    no_sig = scene.org_event(scene.admin)
    del no_sig["sig"]
    assert _error(scene.post(scene.admin, no_sig, extensions={"contract": False}))[1] == {"error": "header_invalid"}
    beyond = scene.org_event(scene.admin, lh=scene.lh + 1)
    assert _error(scene.post(scene.admin, beyond)) == (400, {"error": "header_invalid"})


def test_author_must_be_the_caller(scene: Scene) -> None:
    event = scene.org_event(scene.admin)
    assert _error(scene.post(scene.writer, event)) == (403, {"error": "forbidden"})


def test_unknown_org_is_not_found(scene: Scene) -> None:
    lonely = scene.world.org(scene.stranger)
    lonely.genesis()
    event = lonely.event(scene.stranger, "org.profile", lh=0)
    assert _error(scene.post(scene.stranger, event)) == (404, {"error": "not_found"})


def test_e3_kind_matrix_and_header_rules(scene: Scene) -> None:
    output = scene.wg_event(scene.writer, "session.output", task=new_id(), session=new_id())
    assert _error(scene.post(scene.writer, output)) == (400, {"error": "kind_not_allowed"})
    in_wg = scene.wg_event(scene.admin, "org.profile")
    assert _error(scene.post(scene.admin, in_wg)) == (400, {"error": "kind_not_allowed"})
    foreign_ref = scene.org_event(scene.writer, "member.profile", ref=scene.admin.id)
    assert _error(scene.post(scene.writer, foreign_ref)) == (400, {"error": "header_invalid"})
    prompt_with_session = scene.prompt(scene.writer, new_id(), session=new_id())
    assert _error(scene.post(scene.writer, prompt_with_session)) == (400, {"error": "header_invalid"})
    assert (
        scene.post(scene.writer, scene.org_event(scene.writer, "member.profile", ref=scene.writer.id)).status_code
        == 200
    )


def test_e4_signature(scene: Scene) -> None:
    event = scene.org_event(scene.admin)
    ct = bytearray(b64u_decode(event["ct"]))
    ct[0] ^= 1
    event["ct"] = b64u_encode(bytes(ct))
    assert _error(scene.post(scene.admin, event)) == (401, {"error": "signature_invalid"})
    forged = scene.org_event(scene.admin)
    forged["sig"] = scene.org_event(scene.admin)["sig"]
    assert _error(scene.post(scene.admin, forged))[1] == {"error": "signature_invalid"}


def test_e5_right_at_lh(scene: Scene) -> None:
    reader_prompt = scene.prompt(scene.reader, new_id())
    assert _error(scene.post(scene.reader, reader_prompt)) == (400, {"error": "author_not_writer"})
    member_profile = scene.org_event(scene.writer)
    assert _error(scene.post(scene.writer, member_profile)) == (400, {"error": "author_not_writer"})
    before_grant = scene.prompt(scene.writer, new_id(), lh=4)  # the writer joined, the workgroup does not exist yet
    assert _error(scene.post(scene.writer, before_grant)) == (400, {"error": "author_not_writer"})


def test_e5_right_at_receipt(scene: Scene) -> None:
    prompt = scene.prompt(scene.writer, new_id())
    scene.draft.add("grant", scene.admin, subject=scene.writer.id, wg=scene.wg, access="read")
    assert scene.draft.post(scene.admin).status_code == 200
    assert _error(scene.post(scene.writer, prompt)) == (400, {"error": "author_not_writer"})


def test_e5_archived_workgroup(scene: Scene) -> None:
    scene.draft.add("wg_archive", scene.admin, wg=scene.wg)
    assert scene.draft.post(scene.admin).status_code == 200
    assert _error(scene.post(scene.writer, scene.wg_event(scene.writer))) == (400, {"error": "author_not_writer"})


def test_e5_removed_author(scene: Scene) -> None:
    draft, admin = scene.draft, scene.admin
    event = scene.org_event(scene.stranger)
    assert _error(scene.post(scene.stranger, event)) == (400, {"error": "author_not_writer"})
    profile = scene.org_event(scene.reader, "member.profile", ref=scene.reader.id)
    draft.add("remove", admin, subject=scene.reader.id)
    draft.add("rotate", admin, scope="org", scope_id=draft.id, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=draft.default_wg, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=scene.wg, epoch=2)
    others = [admin, scene.writer]
    devices = [scene.device, scene.other_device]
    wraps = (
        draft.wraps(admin, "org", draft.id, 2, others + devices)
        + draft.wraps(admin, "wg", draft.default_wg, 2, others)
        + draft.wraps(admin, "wg", scene.wg, 2, others + devices)
    )
    assert draft.post(admin, wraps).status_code == 200
    # within the grace period, with the previous epoch: a removed author is never covered
    assert _error(scene.post(scene.reader, profile)) == (400, {"error": "author_not_writer"})


def test_e6_grace_period_boundaries(scene: Scene) -> None:
    clock = scene.world.hub.clock
    old = [scene.wg_event(scene.writer) for _ in range(3)]
    _rotate_wg(scene)
    assert _error(scene.post(scene.writer, scene.wg_event(scene.writer, epoch=3))) == (400, {"error": "epoch_stale"})
    assert scene.post(scene.writer, scene.wg_event(scene.writer, epoch=2)).status_code == 200
    clock.advance(599.999)
    assert scene.post(scene.writer, old[0]).status_code == 200
    clock.advance(0.001)  # exactly 600 s after the rotation
    assert scene.post(scene.writer, old[1]).status_code == 200
    clock.advance(0.001)
    assert _error(scene.post(scene.writer, old[2])) == (400, {"error": "epoch_stale"})
    assert scene.post(scene.writer, scene.wg_event(scene.writer, epoch=2)).status_code == 200


def test_e6_without_a_rotation_only_the_current_epoch(scene: Scene) -> None:
    assert _error(scene.post(scene.writer, scene.wg_event(scene.writer, epoch=2))) == (400, {"error": "epoch_stale"})


def test_e6_grace_needs_the_new_epoch_entitlement(scene: Scene) -> None:
    old = scene.wg_event(scene.writer)
    draft, admin = scene.draft, scene.admin
    draft.add("grant", admin, subject=scene.writer.id, wg=scene.wg, access="read")
    draft.add("grant", admin, subject=scene.writer.id, wg=scene.wg, access="write")
    assert draft.post(admin).status_code == 200
    assert scene.post(scene.writer, old).status_code == 200  # rights at lh and at receipt, current epoch


def test_e7_session_runs_on_the_author_device(scene: Scene) -> None:
    _task, session = scene.start_session()
    assert len(scene.append(scene.device, scene.session_event(scene.device, session))) == 1
    with pytest.raises(HubError) as other:
        scene.append(scene.other_device, scene.session_event(scene.other_device, session))
    assert other.value.code == "author_not_writer"
    with pytest.raises(HubError) as unknown:
        scene.append(scene.device, scene.session_event(scene.device, new_id()))
    assert unknown.value.code == "author_not_writer"
    _task, unplaced = scene.start_session(placed=False)
    with pytest.raises(HubError) as nowhere:
        scene.append(scene.device, scene.session_event(scene.device, unplaced))
    assert nowhere.value.code == "author_not_writer"
    wrong_task = scene.wg_event(scene.device, "session.status", task=new_id(), session=session)
    with pytest.raises(HubError) as mismatch:
        scene.append(scene.device, wrong_task)
    assert mismatch.value.code == "author_not_writer"
    params = scene.wg_event(scene.device, "operation.params", ref=new_id())
    assert scene.append(scene.device, params)[0].scope_seq == 2  # no session kind: no E7


@pytest.mark.parametrize("kind", ["session.error", "workvm.run", "workvm.state"])
def test_e7_knows_every_session_kind_of_the_matrix(scene: Scene, kind: str) -> None:
    """KOR-40: the store takes its session kinds from the matrix, so a kind added there (`session.error`, the work-VM
    kinds) finds the session's device instead of failing E7."""
    assert kind in SESSION_KINDS
    _task, session = scene.start_session()
    assert len(scene.append(scene.device, scene.session_event(scene.device, session, kind))) == 1


def _oversized(event: Json, size: int, author: Any) -> Json:
    """The event with a random `ct` of `size` bytes, correctly signed (the hub never decrypts)."""
    ct = secrets.token_bytes(size)
    sig = author.sign(signed.signature_text(event_format.FORMAT, event["header"], ct))
    return {"header": event["header"], "ct": b64u_encode(ct), "sig": b64u_encode(sig)}


def test_e8_sizes(scene: Scene) -> None:
    _task, session = scene.start_session()
    task = scene.sessions[session][0]
    terminal = scene.session_event(scene.device, session, "session.terminal")
    with pytest.raises(HubError) as too_big:
        scene.append(scene.device, _oversized(terminal, 64 * 1024 + 1, scene.device.identity.sig))
    assert too_big.value.code == "size_exceeded"
    fits = scene.session_event(scene.device, session, "session.terminal")
    assert scene.append(scene.device, _oversized(fits, 64 * 1024, scene.device.identity.sig))[0].task_id == task
    large = scene.wg_event(scene.writer)
    over = _oversized(large, 256 * 1024 + 1, scene.writer.identity.sig)
    assert _error(scene.post(scene.writer, over, extensions={"contract": False})) == (400, {"error": "size_exceeded"})
    limit = _oversized(scene.wg_event(scene.writer), 256 * 1024, scene.writer.identity.sig)
    assert scene.post(scene.writer, limit).status_code == 200


# --- E9, batches ----------------------------------------------------------------------------------------------


def test_e9_replay_and_conflict(scene: Scene, emitted: list[Any]) -> None:
    clock = scene.world.hub.clock
    event = scene.wg_event(scene.writer)
    first = scene.post(scene.writer, event).json()["results"][0]
    _rotate_wg(scene)
    clock.advance(3600)  # far beyond the grace period: a replay is not checked again
    again = scene.post(scene.writer, event)
    assert again.json() == {"results": [{**first, "replayed": True}]}
    assert scene.rows(
        "SELECT count(*) FROM hub.audit WHERE action = 'event.append' AND event_id = %s", event["header"]["id"]
    ) == [(1,)]
    assert len(emitted) == 1
    changed = copy.deepcopy(event)
    changed["header"]["ts"] = format_time(clock())
    assert _error(scene.post(scene.writer, changed, extensions={"contract": False})) == (
        409,
        {"error": "event_conflict"},
    )
    other = scene.wg_event(scene.writer, epoch=2)
    other["header"]["id"] = event["header"]["id"]
    assert _error(scene.post(scene.writer, other))[1] == {"error": "event_conflict"}


def test_e9_inside_one_batch(scene: Scene) -> None:
    event = scene.wg_event(scene.writer)
    response = scene.post(scene.writer, event, event)
    assert [item["replayed"] for item in response.json()["results"]] == [False, True]
    assert {item["scope_seq"] for item in response.json()["results"]} == {1}
    changed = scene.wg_event(scene.writer)
    twin = copy.deepcopy(changed)
    twin["header"]["ref"] = ""
    twin["sig"] = scene.wg_event(scene.writer)["sig"]
    assert _error(scene.post(scene.writer, changed, twin))[1] == {"error": "event_conflict"}


def test_a_batch_is_all_or_nothing(scene: Scene, published: MemoryPublisher, emitted: list[Any]) -> None:
    good = [scene.wg_event(scene.writer), scene.org_event(scene.writer, "member.profile", ref=scene.writer.id)]
    bad = scene.prompt(scene.reader, new_id())
    response = scene.post(scene.writer, *good, scene.wg_event(scene.writer, epoch=2))
    assert _error(response) == (400, {"error": "epoch_stale"})
    assert scene.rows("SELECT count(*) FROM hub.event WHERE author_id = %s", scene.writer.id) == [(0,)]
    assert scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg) == []
    assert (published.items(), emitted) == ([], [])
    assert _error(scene.post(scene.reader, bad))[1] == {"error": "author_not_writer"}
    ok = scene.post(scene.writer, *good)
    assert [item["scope_seq"] for item in ok.json()["results"]] == [1, 4]


def test_request_limits(scene: Scene) -> None:
    events = [{"event": scene.wg_event(scene.writer)} for _ in range(101)]
    response = scene.writer.browser.post("/v1/events", json={"events": events}, extensions={"contract": False})
    assert _error(response)[1]["error"] == "request_invalid"
    empty = scene.writer.browser.post("/v1/events", json={"events": []}, extensions={"contract": False})
    assert _error(empty)[1]["error"] == "request_invalid"
    unknown = scene.writer.browser.post(
        "/v1/events", json={"events": [{"event": scene.wg_event(scene.writer), "x": 1}]}, extensions={"contract": False}
    )
    assert _error(unknown)[1]["error"] == "request_invalid"
    stringly = scene.writer.browser.post(
        "/v1/events",
        json={"events": [{"event": scene.wg_event(scene.writer), "notify": "true"}]},
        extensions={"contract": False},
    )
    assert _error(stringly)[1]["error"] == "request_invalid"
    full = scene.writer.browser.post("/v1/events", json={"events": events[:100]})
    assert [item["scope_seq"] for item in full.json()["results"]] == list(range(1, 101))


def test_a_log_batch_carries_events_of_its_own_org_only(scene: Scene) -> None:
    other = scene.world.public_org(scene.admin)
    foreign = other.event(scene.admin, "org.profile", lh=0)
    scene.draft.add("grant", scene.admin, subject=scene.reader.id, wg=scene.wg, access="write")
    response = scene.draft.post(scene.admin, events=[foreign])
    assert _error(response) == (400, {"error": "header_invalid"})
    own = scene.org_event(scene.admin, lh=scene.lh)
    assert scene.draft.post(scene.admin, events=[own]).status_code == 200


def test_org_routes_announce_their_events(world: Any, published: MemoryPublisher, emitted: list[Any]) -> None:
    draft = world.public_org()
    assert [ids["kind"] for _name, ids in emitted] == ["org.profile", "wg.profile", "member.profile"]
    assert [item.frame for item in published.items()].count("event") == 3
    assert {str(ids["org_id"]) for _name, ids in emitted} == {draft.id}


def test_a_failing_fan_out_never_fails_the_committed_request(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any) -> None:
        raise RuntimeError("fan-out down")

    monkeypatch.setattr("agent_hub.events.notify.publish", broken)
    event = scene.wg_event(scene.writer)
    assert scene.post(scene.writer, event).status_code == 200
    assert "announcing event" in caplog.text
    assert scene.rows("SELECT count(*) FROM hub.event WHERE id = %s", event["header"]["id"]) == [(1,)]
