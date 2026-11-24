"""U-HUB-contexts (checkpoints): upload, commit, download, quotas, pruning, expiry and deletion of context checkpoints
(ADR-072 sections 3, 5 and 6; 03-api.md section 6.1)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from agent_hub.contexts import jobs, store
from agent_hub.crypto import context
from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.device.access import device_view
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.conftest import HubDatabase
from tests.platform.hub.contexts.support import NOT_CONTRACT, Checkpoints, Limits, error
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import audit_rows


def stored(checkpoints: Checkpoints) -> list[str]:
    return [str(row[0]) for row in checkpoints.outbox.desk.rows("SELECT id FROM hub.context_object ORDER BY id")]


def download(checkpoints: Checkpoints, object_id: str, device: Actor | None = None) -> tuple[int, int]:
    """The status of the object and of its first chunk."""
    client = checkpoints.client(device)
    return (
        client.get(f"/v1/device/contexts/{object_id}").status_code,
        client.get(f"/v1/device/contexts/{object_id}/chunks/0").status_code,
    )


def listed(checkpoints: Checkpoints, task: str, client: Any = None) -> list[str]:
    response = (client or checkpoints.scene.reader.browser).get(f"/v1/tasks/{task}/contexts")
    assert response.status_code == 200, response.text
    return [row["object_id"] for row in response.json()["contexts"]]


# --- upload, commit, download -----------------------------------------------------------------------------------------


def test_a_checkpoint_is_uploaded_committed_and_read_by_another_device(checkpoints: Checkpoints) -> None:
    desk = checkpoints.outbox.desk
    task, session = checkpoints.outbox.session()
    sealed = checkpoints.seal(task, session)
    # chunks may arrive in any order; the identical chunk again is a replay
    for index, replayed in ((1, False), (0, False), (0, True)):
        response = checkpoints.put(sealed.object_id, index, session, sealed.chunks[index])
        assert error(response) == (200, {"replayed": replayed})
    # nobody sees an upload that is not committed: not the list, not the device state, not a download
    other = checkpoints.scene.other_device
    assert listed(checkpoints, task) == []
    assert download(checkpoints, sealed.object_id) == (404, 404)
    created = format_time(checkpoints.outbox.hub.clock())
    checkpoints.outbox.hub.clock.advance(2)
    completed = format_time(checkpoints.outbox.hub.clock())
    assert error(checkpoints.commit(sealed)) == (200, {"completed_at": completed, "replayed": False})
    checkpoints.outbox.hub.clock.advance(2)
    assert error(checkpoints.commit(sealed)) == (200, {"completed_at": completed, "replayed": True})
    assert audit_rows(desk, "session.context_saved") == [
        (
            "device",
            UUID(checkpoints.outbox.device.id),
            sealed.object_id,
            {"chunks": 2, "size": sealed.manifest["size"], "pruned": 0},
        )
    ]
    # any device of the workgroup reads the complete checkpoint
    for device in (checkpoints.outbox.device, other):
        client = checkpoints.client(device)
        assert client.get(f"/v1/device/contexts/{sealed.object_id}").json() == {
            "manifest": sealed.manifest,
            "sig": sealed.sig,
            "created_at": created,
            "completed_at": completed,
        }
        for index, chunk in enumerate(sealed.chunks):
            found = client.get(f"/v1/device/contexts/{sealed.object_id}/chunks/{index}")
            assert found.json() == {"ct": b64u_encode(chunk)}
        assert client.get(f"/v1/device/contexts/{sealed.object_id}/chunks/2").status_code == 404
    # after the commit the stored chunk is still a replay, anything else a conflict
    assert error(checkpoints.put(sealed.object_id, 0, session, sealed.chunks[0])) == (200, {"replayed": True})
    assert error(checkpoints.put(sealed.object_id, 2, session, sealed.chunks[0])) == (409, {"error": "id_conflict"})
    assert error(checkpoints.commit(sealed.resigned(checkpoints.outbox.device, ts="2026-11-23T12:00:00.000Z"))) == (
        409,
        {"error": "id_conflict"},
    )
    assert error(checkpoints.commit(replace(sealed, sig=b64u_encode(bytes(64))))) == (409, {"error": "id_conflict"})


def test_chunk_refusals(checkpoints: Checkpoints) -> None:
    task, session = checkpoints.outbox.session()
    sealed = checkpoints.seal(task, session)
    chunk = sealed.chunks[0]
    put = checkpoints.put
    loose: dict[str, Any] = {"extensions": NOT_CONTRACT}
    ct = {"error": "request_invalid", "detail": {"pointers": ["/ct"]}}
    assert error(put(sealed.object_id, 0, session, "AA=", **loose)) == (400, ct)
    assert error(put(sealed.object_id, 0, session, bytes(context.MIN_CT_BYTES - 1), **loose)) == (400, ct)
    too_large = put(sealed.object_id, 0, session, bytes(context.CHUNK_CT_BYTES + 1), **loose)
    assert error(too_large) == (413, {"error": "size_exceeded"})
    for index in ("01", "-1", "1000000", "x"):
        assert error(put(sealed.object_id, index, session, chunk, **loose)) == (404, {"error": "not_found"})
    assert error(put("nothing", 0, session, chunk, **loose)) == (404, {"error": "not_found"})
    assert error(put(sealed.object_id, 0, new_id(), chunk)) == (404, {"error": "not_found"})
    # the largest chunk the format allows fits the body limit
    assert put(new_id(), 0, session, bytes(context.CHUNK_CT_BYTES)).status_code == 200
    # other bytes at a stored index, another session or another device under the same object
    assert put(sealed.object_id, 0, session, chunk).status_code == 200
    conflict = (409, {"error": "id_conflict"})
    assert error(put(sealed.object_id, 0, session, sealed.chunks[1])) == conflict
    other = checkpoints.scene.other_device
    _task, second = checkpoints.outbox.session()
    _task, foreign = checkpoints.outbox.session(other)
    assert error(put(sealed.object_id, 1, second, sealed.chunks[1])) == conflict
    assert error(put(sealed.object_id, 1, foreign, sealed.chunks[1], other)) == conflict


def test_only_the_device_of_the_session_in_an_assigned_workgroup_uploads(checkpoints: Checkpoints) -> None:
    desk, device = checkpoints.outbox.desk, checkpoints.outbox.device
    forbidden = (403, {"error": "forbidden"})
    task, session = checkpoints.outbox.session()
    sealed = checkpoints.seal(task, session)
    # assigned to the workgroup, but the session runs elsewhere; never placed; placed here but in another workgroup
    assert error(checkpoints.put(new_id(), 0, session, sealed.chunks[0], checkpoints.scene.other_device)) == forbidden
    _task, unplaced = checkpoints.scene.start_session(placed=False)
    assert error(checkpoints.put(new_id(), 0, unplaced, sealed.chunks[0])) == forbidden
    _task, outside = checkpoints.scene.start_session(wg=checkpoints.scene.draft.default_wg)
    assert error(checkpoints.put(new_id(), 0, outside, sealed.chunks[0])) == forbidden
    # an ended session still uploads (its final checkpoint), a subagent session never
    desk.execute("UPDATE hub.session SET state = 'stopped', ended_at = %s WHERE id = %s", desk.hub.clock(), session)
    checkpoints.upload(sealed, session)
    child = new_id()
    desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'working', %s)",
        child,
        task,
        desk.org,
        desk.wg,
        desk.scene.writer.id,
        device.id,
        session,
        desk.hub.clock(),
    )
    assert error(checkpoints.put(new_id(), 0, child, sealed.chunks[0])) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/session_id"]}},
    )
    # only the author commits, and only while it is assigned to the workgroup
    assert error(checkpoints.commit(sealed, checkpoints.scene.other_device)) == forbidden
    default = checkpoints.scene.draft.default_wg
    desk.execute("UPDATE hub.context_object SET wg_id = %s WHERE id = %s", default, sealed.object_id)
    assert error(checkpoints.commit(sealed)) == forbidden
    desk.execute("UPDATE hub.context_object SET wg_id = %s WHERE id = %s", desk.wg, sealed.object_id)
    assert checkpoints.commit(sealed).status_code == 200
    # a device that is not assigned to the checkpoint's workgroup does not learn that it exists
    desk.execute("UPDATE hub.context_object SET wg_id = %s WHERE id = %s", default, sealed.object_id)
    assert download(checkpoints, sealed.object_id) == (404, 404)
    assert download(checkpoints, new_id()) == (404, 404)


def test_commit_refusals(checkpoints: Checkpoints) -> None:
    device = checkpoints.outbox.device
    task, session = checkpoints.outbox.session()
    sealed = checkpoints.seal(task, session)

    def refused(**changes: object) -> tuple[int, Any]:
        status, body = error(checkpoints.commit(sealed.resigned(device, **changes)))
        return status, body.get("detail", {}).get("pointers", [body["error"]])

    assert error(checkpoints.commit(sealed)) == (404, {"error": "not_found"})
    checkpoints.upload(sealed, session)
    for key in ("object", "org", "wg", "task", "session", "author"):
        assert refused(**{key: new_id()}) == (400, [f"/manifest/{key}"])
    assert refused(epoch=2) == (400, ["/manifest/epoch"])
    assert refused(chunks=3, size=200) == (400, ["/manifest/chunks"])
    assert refused(size=int(str(sealed.manifest["size"])) + 1) == (400, ["/manifest/size"])
    assert refused(digest=b64u_encode(bytes(32))) == (400, ["/manifest/digest"])
    # the manifest's format and the signature of the calling device
    for change, code in (({"v": 2}, "header_invalid"), ({"ts": "2026-11-23T12:00:00.000Zé"}, "non_ascii")):
        broken = replace(sealed, manifest={**sealed.manifest, **change})
        assert error(checkpoints.commit(broken, extensions=NOT_CONTRACT)) == (400, {"error": code})
    by_other = sealed.resigned(checkpoints.scene.other_device)
    assert error(checkpoints.commit(by_other)) == (401, {"error": "signature_invalid"})
    unsigned = replace(sealed, sig="no signature")
    assert error(checkpoints.commit(unsigned, extensions=NOT_CONTRACT)) == (401, {"error": "signature_invalid"})
    assert listed(checkpoints, task) == []
    assert checkpoints.commit(sealed).status_code == 200


def test_a_half_upload_is_never_sealed(checkpoints: Checkpoints) -> None:
    """A missing chunk - at the end or in the middle - never becomes a checkpoint (ADR-072 section 3)."""
    device = checkpoints.outbox.device
    task, session = checkpoints.outbox.session()
    sealed = checkpoints.seal(task, session, [b"one", b"two", b"three"])
    pointer = (400, {"error": "request_invalid", "detail": {"pointers": ["/manifest/chunks"]}})
    checkpoints.put(sealed.object_id, 0, session, sealed.chunks[0])
    checkpoints.put(sealed.object_id, 2, session, sealed.chunks[2])
    assert error(checkpoints.commit(sealed)) == pointer
    # a manifest over just the two stored chunks does not fit their indexes either
    present = [sealed.chunks[0], sealed.chunks[2]]
    gap = sealed.resigned(device, chunks=2, size=sum(map(len, present)), digest=context.digest(present))
    assert error(checkpoints.commit(gap)) == pointer
    assert listed(checkpoints, task) == []
    checkpoints.put(sealed.object_id, 1, session, sealed.chunks[1])
    assert checkpoints.commit(sealed).status_code == 200


# --- quotas -----------------------------------------------------------------------------------------------------------


def refusals(checkpoints: Checkpoints) -> list[tuple[Any, ...]]:
    """`(result, session, target, detail)` of the audit rows `session.context_refused` (by the calling device)."""
    rows = audit_rows(checkpoints.outbox.desk, "session.context_refused")
    assert {(row[0], str(row[1])) for row in rows} <= {("device", checkpoints.outbox.device.id)}
    details = checkpoints.outbox.desk.rows(
        "SELECT result, session_id::text, target_kind FROM hub.audit WHERE action = 'session.context_refused'"
        " ORDER BY seq"
    )
    return [(*found, row[2], row[3]) for found, row in zip(details, rows, strict=True)]


def test_the_object_quota(checkpoints: Checkpoints, limits: Limits) -> None:
    task, session = checkpoints.outbox.session()
    sealed = checkpoints.seal(task, session)
    first, second = sealed.chunks
    limits(context_max_object_bytes=len(first) + len(second) - 1)
    assert checkpoints.put(sealed.object_id, 0, session, first).status_code == 200
    quota = {"scope": "object", "limit": len(first) + len(second) - 1, "used": len(first)}
    refused = checkpoints.put(sealed.object_id, 1, session, second)
    assert error(refused) == (413, {"error": "quota_exceeded", "detail": {"quota": quota}})
    # the refusal is audited although its transaction was rolled back (review 1 decision 6)
    detail = {**quota, "reason": "quota_exceeded", "requested": len(second)}
    assert refusals(checkpoints) == [("denied", session, "context", sealed.object_id, detail)]
    limits(context_max_object_bytes=len(first) + len(second))
    assert checkpoints.put(sealed.object_id, 1, session, second).status_code == 200
    assert checkpoints.commit(sealed).status_code == 200


def test_the_organisation_quota_counts_every_object_of_the_org(checkpoints: Checkpoints, limits: Limits) -> None:
    task, session = checkpoints.outbox.session()
    saved = checkpoints.saved(task, session)
    used = sum(map(len, saved.chunks))
    other_task, other_session = checkpoints.outbox.session()
    sealed = checkpoints.seal(other_task, other_session, [b"x"])
    limits(context_org_bytes=used + len(sealed.chunks[0]) - 1)
    refused = checkpoints.put(sealed.object_id, 0, other_session, sealed.chunks[0])
    quota = {"scope": "org", "limit": used + len(sealed.chunks[0]) - 1, "used": used}
    assert error(refused) == (413, {"error": "quota_exceeded", "detail": {"quota": quota}})
    # the refusal leaves no object behind and the older checkpoint untouched, but an audit row
    assert stored(checkpoints) == [saved.object_id]
    detail = {**quota, "reason": "quota_exceeded", "requested": len(sealed.chunks[0])}
    assert refusals(checkpoints) == [("denied", other_session, "context", sealed.object_id, detail)]
    limits(context_org_bytes=used + len(sealed.chunks[0]))
    assert checkpoints.put(sealed.object_id, 0, other_session, sealed.chunks[0]).status_code == 200


# --- pruning and order ------------------------------------------------------------------------------------------------


def test_older_checkpoints_of_the_task_are_pruned(checkpoints: Checkpoints, limits: Limits) -> None:
    limits(context_keep=2)
    task, session = checkpoints.outbox.session()
    other_task, other_session = checkpoints.outbox.session()
    foreign = checkpoints.saved(other_task, other_session)
    pending = checkpoints.seal(task, session)
    checkpoints.put(pending.object_id, 0, session, pending.chunks[0])
    ids = []
    for _ in range(4):
        checkpoints.outbox.hub.clock.advance(1)
        ids.append(checkpoints.saved(task, session).object_id)
    assert listed(checkpoints, task) == [ids[3], ids[2]]
    # neither another task's checkpoint nor an upload in progress is touched; the chunks go with their object
    assert set(stored(checkpoints)) == {foreign.object_id, pending.object_id, ids[2], ids[3]}
    assert checkpoints.outbox.desk.rows("SELECT count(*) FROM hub.context_chunk") == [(7,)]
    assert [row[3]["pruned"] for row in audit_rows(checkpoints.outbox.desk, "session.context_saved")] == [0, 0, 0, 1, 1]
    assert download(checkpoints, ids[0]) == (404, 404)


def test_a_late_checkpoint_of_an_earlier_session_never_supersedes_its_successor(
    checkpoints: Checkpoints, limits: Limits
) -> None:
    """A device that was offline while the task went on elsewhere uploads its old checkpoint last."""
    limits(context_keep=1)
    desk = checkpoints.outbox.desk
    task, first = checkpoints.outbox.session()
    old = checkpoints.seal(task, first)
    checkpoints.upload(old, first)
    desk.hub.clock.advance(60)
    other = checkpoints.scene.other_device
    _other_task, second = checkpoints.outbox.session(other)
    desk.execute("UPDATE hub.session SET task_id = %s WHERE id = %s", task, second)
    current = checkpoints.saved(task, second, device=other)
    desk.hub.clock.advance(60)
    assert checkpoints.commit(old).status_code == 200
    assert listed(checkpoints, task) == [current.object_id, old.object_id]
    with desk.hub.pool.connection() as connection:
        newest = store.newest(connection, task)
        assert newest is not None
        assert newest["object_id"] == current.object_id
        assert store.newest(connection, new_id()) is None
    # the late one goes with the next commit
    desk.hub.clock.advance(1)
    following = checkpoints.saved(task, second, device=other)
    assert listed(checkpoints, task) == [following.object_id]


def test_within_a_session_the_object_id_orders_never_the_commit(checkpoints: Checkpoints) -> None:
    """Review 1 decision 10: the order is (creation of the session at the hub, object ID), not the commit time."""
    task, session = checkpoints.outbox.session()
    early, late = checkpoints.seal(task, session), checkpoints.seal(task, session)
    for sealed in (early, late):
        checkpoints.upload(sealed, session)
    assert checkpoints.commit(late).status_code == 200
    checkpoints.outbox.hub.clock.advance(60)
    assert checkpoints.commit(early).status_code == 200
    assert listed(checkpoints, task) == [late.object_id, early.object_id]
    with checkpoints.outbox.hub.pool.connection() as connection:
        newest = store.newest(connection, task)
    assert newest is not None
    assert newest["object_id"] == late.object_id


def test_two_commits_of_a_task_wait_for_each_other(checkpoints: Checkpoints) -> None:
    """Each commit prunes with the other's checkpoint in view: never more than `keep` remain, never none."""
    hub, device = checkpoints.outbox.hub, checkpoints.outbox.device
    task, session = checkpoints.outbox.session()
    both = [checkpoints.seal(task, session) for _ in range(2)]
    for sealed in both:
        checkpoints.upload(sealed, session)

    def commit(connection: Any, number: int) -> None:
        store.commit(
            connection,
            device_view(connection, device.id, hub.config.platform_root_key),
            UUID(device.id),
            object_id=UUID(both[number].object_id),
            manifest_value=both[number].manifest,
            sig=both[number].sig,
            now=hub.clock() + timedelta(seconds=number),
            keep=1,
        )

    def second() -> None:
        with hub.pool.connection() as connection, connection.transaction():
            commit(connection, 1)

    thread = threading.Thread(target=second)
    with hub.pool.connection() as connection, connection.transaction():
        commit(connection, 0)
        thread.start()
        deadline = time.monotonic() + 20
        waiting = 0
        while waiting == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
            row = connection.execute(
                "SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid = %s"
                " AND database = (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())",
                (store.COMMIT_LOCK,),
            ).fetchone()
            waiting = row[0] if row else 0
        assert waiting == 1
    thread.join(20)
    assert not thread.is_alive()
    assert listed(checkpoints, task) == [both[1].object_id]


# --- deletion ---------------------------------------------------------------------------------------------------------


def test_abandoned_uploads_expire(checkpoints: Checkpoints, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="agent_hub.contexts")
    clock = checkpoints.outbox.hub.clock
    hub = SimpleNamespace(pool=checkpoints.outbox.hub.pool, clock=clock)
    (job,) = jobs.JOBS
    assert (job.name, job.interval_seconds, job.run) == ("contexts.expire", 600.0, jobs.expire)
    task, session = checkpoints.outbox.session()
    saved = checkpoints.saved(task, session)
    left = checkpoints.seal(task, session)
    checkpoints.put(left.object_id, 0, session, left.chunks[0])
    clock.advance(store.STALE_UPLOAD.total_seconds() - 1)
    going_on = checkpoints.seal(task, session)
    checkpoints.put(going_on.object_id, 0, session, going_on.chunks[0])
    clock.advance(1)
    assert jobs.expire(hub) == 0
    assert caplog.messages == []
    clock.advance(1)
    assert jobs.expire(hub) == 1
    assert caplog.messages == ["contexts.expire removed=1"]
    assert set(stored(checkpoints)) == {saved.object_id, going_on.object_id}
    # the abandoned object can be uploaded again from the start
    checkpoints.upload(left, session)
    assert checkpoints.commit(left).status_code == 200


def test_everything_goes_with_the_task(checkpoints: Checkpoints, migrated_db: HubDatabase) -> None:
    desk = checkpoints.outbox.desk
    task, session = checkpoints.outbox.session()
    other_task, other_session = checkpoints.outbox.session()
    kept = checkpoints.saved(other_task, other_session)
    checkpoints.saved(task, session)
    link = {"session_id": session, "epoch": 1, "tag": b64u_encode(bytes(32))}
    assert checkpoints.client().post("/v1/device/pr-links", json=link).status_code == 200
    with migrated_db.admin() as connection:
        connection.execute("DELETE FROM hub.task WHERE id = %s", (task,))
    assert stored(checkpoints) == [kept.object_id]
    assert desk.rows("SELECT DISTINCT object_id::text FROM hub.context_chunk") == [(kept.object_id,)]
    assert desk.rows("SELECT count(*) FROM hub.pr_link") == [(0,)]


# --- the list of a task -----------------------------------------------------------------------------------------------


def test_the_list_for_a_browser_and_for_the_users_agent(checkpoints: Checkpoints) -> None:
    hub, scene = checkpoints.outbox.hub, checkpoints.scene
    task, session = checkpoints.outbox.session()
    created = format_time(hub.clock())
    sealed = checkpoints.saved(task, session)
    expected = {
        "contexts": [
            {
                "object_id": sealed.object_id,
                "session_id": session,
                "author_id": checkpoints.outbox.device.id,
                "epoch": 1,
                "chunks": 2,
                "size": sealed.manifest["size"],
                "digest": sealed.manifest["digest"],
                "created_at": created,
                "completed_at": created,
            }
        ]
    }
    path = f"/v1/tasks/{task}/contexts"
    agent = agent_client(hub, UUID(scene.reader.id))
    for client in (scene.writer.browser, scene.reader.browser, agent):
        assert error(client.get(path)) == (200, expected)
    # exactly the user's rights: a stranger and his agent read nothing
    for client in (scene.stranger.browser, agent_client(hub, UUID(scene.stranger.id))):
        assert client.get(path).status_code in (403, 404)
    assert error(scene.writer.browser.get(f"/v1/tasks/{new_id()}/contexts")) == (404, {"error": "not_found"})
    assert scene.writer.browser.get("/v1/tasks/x/contexts", extensions=NOT_CONTRACT).status_code == 404
    hub.flush_read_log()
    assert len(audit_rows(checkpoints.outbox.desk, "read.task.contexts")) >= 3
