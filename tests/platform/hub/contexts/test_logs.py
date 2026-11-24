"""U-HUB-contexts (logs): logs of test runs and work VM jobs as objects of kind `log` beside the checkpoints - upload,
commit, the members' read routes, quotas and pruning of their own (ADR-080 section 3; 03-api.md sections 4.6, 6.1)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from agent_hub.crypto import context, runlog
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.contexts.support import NOT_CONTRACT, WK, Checkpoints, Limits, Sealed, error
from tests.platform.hub.orgs.support import new_id
from tests.platform.hub.tasks.support import audit_rows

NOT_FOUND = (404, {"error": "not_found"})
CONFLICT = (409, {"error": "id_conflict"})


def kinds(checkpoints: Checkpoints) -> dict[str, str]:
    rows = checkpoints.outbox.desk.rows("SELECT id::text, kind FROM hub.context_object")
    return {row[0]: row[1] for row in rows}


def child_session(checkpoints: Checkpoints, task: str, parent: str) -> str:
    """A subagent session (depth 1) of the main session on the scene's device."""
    desk, child = checkpoints.outbox.desk, new_id()
    desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'working', %s)",
        child,
        task,
        desk.org,
        desk.wg,
        desk.scene.writer.id,
        checkpoints.outbox.device.id,
        parent,
        desk.hub.clock(),
    )
    return child


def read_log(checkpoints: Checkpoints, task: str, sealed: Sealed, client: Any = None) -> bytes:
    """The output of a complete log as a member reads it through the browser routes."""
    browser = client or checkpoints.scene.reader.browser
    found = browser.get(f"/v1/tasks/{task}/logs/{sealed.object_id}")
    assert found.status_code == 200, found.text
    body = found.json()
    chunks = []
    for index in range(int(body["manifest"]["chunks"])):
        chunk = browser.get(f"/v1/tasks/{task}/logs/{sealed.object_id}/chunks/{index}")
        assert chunk.status_code == 200, chunk.text
        chunks.append(chunk.json()["ct"])
    return runlog.open_log(
        WK,
        body["manifest"],
        body["sig"],
        checkpoints.outbox.device.identity.sig_public(),
        [b64u_decode(item) for item in chunks],
    )


# --- upload, commit, reading ------------------------------------------------------------------------------------------


def test_a_log_is_uploaded_committed_and_read_by_the_members(checkpoints: Checkpoints) -> None:
    hub, scene, desk = checkpoints.outbox.hub, checkpoints.scene, checkpoints.outbox.desk
    task, session = checkpoints.outbox.session()
    output = bytes(range(256)) * 1025 + b"tail"  # two chunks: one full piece of 256 KiB and the rest
    sealed = checkpoints.seal_log(task, session, output, total=len(output) + 7)
    assert len(sealed.chunks) == 2
    for index, replayed in ((1, False), (0, False), (0, True)):
        response = checkpoints.put(sealed.object_id, index, session, sealed.chunks[index], kind="log")
        assert error(response) == (200, {"replayed": replayed})
    # an upload in progress is read by nobody
    assert error(scene.reader.browser.get(f"/v1/tasks/{task}/logs/{sealed.object_id}")) == NOT_FOUND
    assert error(scene.reader.browser.get(f"/v1/tasks/{task}/logs/{sealed.object_id}/chunks/0")) == NOT_FOUND
    created = format_time(hub.clock())
    hub.clock.advance(2)
    completed = format_time(hub.clock())
    assert error(checkpoints.commit(sealed)) == (200, {"completed_at": completed, "replayed": False})
    assert error(checkpoints.commit(sealed)) == (200, {"completed_at": completed, "replayed": True})
    assert kinds(checkpoints) == {sealed.object_id: "log"}
    assert audit_rows(desk, "session.context_saved") == [
        (
            "device",
            UUID(checkpoints.outbox.device.id),
            sealed.object_id,
            {"chunks": 2, "size": sealed.manifest["size"], "pruned": 0},
        )
    ]
    assert desk.rows("SELECT target_kind FROM hub.audit WHERE action = 'session.context_saved'") == [("log",)]
    # every member with `task:read` - a browser or the user's agent - reads manifest and chunks; the output opens
    expected = {"manifest": sealed.manifest, "sig": sealed.sig, "created_at": created, "completed_at": completed}
    agent = agent_client(hub, UUID(scene.reader.id))
    for client in (scene.writer.browser, scene.reader.browser, agent):
        assert error(client.get(f"/v1/tasks/{task}/logs/{sealed.object_id}")) == (200, expected)
        assert read_log(checkpoints, task, sealed, client) == output
    assert error(scene.reader.browser.get(f"/v1/tasks/{task}/logs/{sealed.object_id}/chunks/1")) == (
        200,
        {"ct": b64u_encode(sealed.chunks[1])},
    )
    hub.flush_read_log()
    readers = {str(scene.writer.id), str(scene.reader.id)}
    for action in ("read.task.log", "read.task.log.chunk"):
        rows = audit_rows(desk, action)
        assert rows, action
        assert {str(row[1]) for row in rows} <= readers, action
    # a log is never a checkpoint: not listed, not offered to a device for a restore, not the device state's context
    assert scene.reader.browser.get(f"/v1/tasks/{task}/contexts").json() == {"contexts": []}
    client = checkpoints.client()
    assert client.get(f"/v1/device/contexts/{sealed.object_id}").status_code == 404
    assert client.get(f"/v1/device/contexts/{sealed.object_id}/chunks/0").status_code == 404
    item = next(row for row in client.get("/v1/device/state").json()["sessions"] if row["session_id"] == session)
    assert "context" not in item


def test_the_read_routes_name_only_a_complete_log_of_the_task(checkpoints: Checkpoints) -> None:
    scene = checkpoints.scene
    task, session = checkpoints.outbox.session()
    other_task, other_session = checkpoints.outbox.session()
    log = checkpoints.logged(task, session)
    checkpoint = checkpoints.saved(task, session)
    foreign = checkpoints.logged(other_task, other_session)
    pending = checkpoints.seal_log(task, session)
    checkpoints.put(pending.object_id, 0, session, pending.chunks[0], kind="log")
    browser = scene.writer.browser

    def both(task_id: str, object_id: str) -> tuple[tuple[int, Any], tuple[int, Any]]:
        return (
            error(browser.get(f"/v1/tasks/{task_id}/logs/{object_id}")),
            error(browser.get(f"/v1/tasks/{task_id}/logs/{object_id}/chunks/0")),
        )

    for task_id, object_id in (
        (new_id(), log.object_id),  # an unknown task
        (task, checkpoint.object_id),  # a checkpoint
        (task, pending.object_id),  # an upload in progress
        (task, foreign.object_id),  # another task's log
        (other_task, log.object_id),
        (task, new_id()),
    ):
        assert both(task_id, object_id) == (NOT_FOUND, NOT_FOUND), (task_id, object_id)
    assert error(browser.get(f"/v1/tasks/{task}/logs/{log.object_id}/chunks/1")) == NOT_FOUND
    loose: dict[str, Any] = {"extensions": NOT_CONTRACT}
    for path in (
        f"/v1/tasks/x/logs/{log.object_id}",
        f"/v1/tasks/{task}/logs/x",
        f"/v1/tasks/{task}/logs/{log.object_id}/chunks/01",
        f"/v1/tasks/{task}/logs/{log.object_id}/chunks/-1",
        f"/v1/tasks/{task}/logs/{log.object_id}/chunks/x",
    ):
        assert browser.get(path, **loose).status_code == 404, path
    # without `task:read` on the task's workgroup nothing is named (the read rule of `rights.require`)
    for client in (scene.stranger.browser, agent_client(checkpoints.outbox.hub, UUID(scene.stranger.id))):
        assert client.get(f"/v1/tasks/{task}/logs/{log.object_id}").status_code in (403, 404)
        assert client.get(f"/v1/tasks/{task}/logs/{log.object_id}/chunks/0").status_code in (403, 404)
    assert both(task, log.object_id)[0][0] == 200


# --- upload rules -----------------------------------------------------------------------------------------------------


def test_a_log_may_belong_to_a_subagent_a_checkpoint_never(checkpoints: Checkpoints) -> None:
    task, session = checkpoints.outbox.session()
    child = child_session(checkpoints, task, session)
    sealed = checkpoints.seal_log(task, child)
    checkpoints.upload(sealed, child)
    assert checkpoints.commit(sealed).status_code == 200
    assert read_log(checkpoints, task, sealed) == b"collected 2 items\n2 passed\n"
    checkpoint = checkpoints.seal(task, child)
    assert error(checkpoints.put(checkpoint.object_id, 0, child, checkpoint.chunks[0])) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/session_id"]}},
    )
    explicit = checkpoints.put(checkpoint.object_id, 0, child, checkpoint.chunks[0], kind="context")
    assert error(explicit) == (400, {"error": "request_invalid", "detail": {"pointers": ["/session_id"]}})


def test_the_kind_of_an_object_is_fixed_by_its_first_chunk(checkpoints: Checkpoints) -> None:
    task, session = checkpoints.outbox.session()
    log = checkpoints.seal_log(task, session, b"x" * (runlog.CHUNK_BYTES + 1))
    checkpoint = checkpoints.seal(task, session)
    put = checkpoints.put
    assert put(log.object_id, 0, session, log.chunks[0], kind="log").status_code == 200
    # another kind for the same object - a later chunk or the stored one again - is a conflict
    assert error(put(log.object_id, 1, session, log.chunks[1])) == CONFLICT
    assert error(put(log.object_id, 1, session, log.chunks[1], kind="context")) == CONFLICT
    assert error(put(log.object_id, 0, session, log.chunks[0])) == CONFLICT
    assert put(checkpoint.object_id, 0, session, checkpoint.chunks[0], kind="context").status_code == 200
    assert error(put(checkpoint.object_id, 1, session, checkpoint.chunks[1], kind="log")) == CONFLICT
    assert put(checkpoint.object_id, 1, session, checkpoint.chunks[1]).status_code == 200
    assert put(log.object_id, 1, session, log.chunks[1], kind="log").status_code == 200
    assert kinds(checkpoints) == {log.object_id: "log", checkpoint.object_id: "context"}
    # an unknown kind is no request
    unknown = {"session_id": session, "ct": b64u_encode(log.chunks[0]), "kind": "trace"}
    response = checkpoints.client().put(
        f"/v1/device/contexts/{new_id()}/chunks/0", json=unknown, extensions=NOT_CONTRACT
    )
    assert response.status_code == 400
    for sealed in (log, checkpoint):
        assert checkpoints.commit(sealed).status_code == 200


def test_the_chunk_limit_is_that_of_the_kind(checkpoints: Checkpoints) -> None:
    _task, session = checkpoints.outbox.session()
    put = checkpoints.put
    largest, larger = bytes(runlog.CHUNK_CT_BYTES), bytes(runlog.CHUNK_CT_BYTES + 1)
    assert put(new_id(), 0, session, largest, kind="log").status_code == 200
    too_large = put(new_id(), 0, session, larger, kind="log", extensions=NOT_CONTRACT)
    assert error(too_large) == (413, {"error": "size_exceeded"})
    assert put(new_id(), 0, session, larger).status_code == 200  # a checkpoint chunk is up to 2 MiB
    assert len(larger) < context.CHUNK_CT_BYTES
    short = put(new_id(), 0, session, bytes(context.MIN_CT_BYTES - 1), kind="log", extensions=NOT_CONTRACT)
    assert error(short) == (400, {"error": "request_invalid", "detail": {"pointers": ["/ct"]}})


def test_the_organisation_quota_counts_each_kind_on_its_own(checkpoints: Checkpoints, limits: Limits) -> None:
    task, session = checkpoints.outbox.session()
    saved = checkpoints.saved(task, session)
    logged = checkpoints.logged(task, session)
    log_used, context_used = sum(map(len, logged.chunks)), sum(map(len, saved.chunks))
    log = checkpoints.seal_log(task, session, b"y")
    checkpoint = checkpoints.seal(task, session, [b"z"])
    (log_chunk,), (context_chunk,) = log.chunks, checkpoint.chunks
    # each kind just misses its own quota, while the other kind's bytes would fit it
    limits(log_org_bytes=log_used + len(log_chunk) - 1, context_org_bytes=context_used + len(context_chunk) - 1)
    refused = checkpoints.put(log.object_id, 0, session, log_chunk, kind="log")
    quota = {"scope": "org", "limit": log_used + len(log_chunk) - 1, "used": log_used}
    assert error(refused) == (413, {"error": "quota_exceeded", "detail": {"quota": quota}})
    refused = checkpoints.put(checkpoint.object_id, 0, session, context_chunk)
    quota = {"scope": "org", "limit": context_used + len(context_chunk) - 1, "used": context_used}
    assert error(refused) == (413, {"error": "quota_exceeded", "detail": {"quota": quota}})
    assert set(kinds(checkpoints)) == {saved.object_id, logged.object_id}
    rows = checkpoints.outbox.desk.rows(
        "SELECT target_id FROM hub.audit WHERE action = 'session.context_refused' ORDER BY seq"
    )
    assert [row[0] for row in rows] == [log.object_id, checkpoint.object_id]
    limits(log_org_bytes=log_used + len(log_chunk), context_org_bytes=context_used + len(context_chunk))
    assert checkpoints.put(log.object_id, 0, session, log_chunk, kind="log").status_code == 200
    assert checkpoints.put(checkpoint.object_id, 0, session, context_chunk).status_code == 200


# --- commit -----------------------------------------------------------------------------------------------------------


def test_a_log_commits_only_with_a_log_manifest(checkpoints: Checkpoints) -> None:
    device = checkpoints.outbox.device
    task, session = checkpoints.outbox.session()
    log = checkpoints.seal_log(task, session)
    checkpoints.upload(log, session)
    # a checkpoint manifest over the same chunks (signed under the checkpoint label) is refused for a log ...
    as_checkpoint = {key: value for key, value in log.manifest.items() if key in context.MANIFEST_KEYS}
    swapped = replace(log, manifest=as_checkpoint, sig=context.sign_manifest(device.identity.sig, as_checkpoint))
    assert error(checkpoints.commit(swapped, extensions=NOT_CONTRACT)) == (400, {"error": "header_invalid"})
    # ... and a log manifest for a checkpoint
    checkpoint = checkpoints.seal(task, session, [b"one"])
    checkpoints.upload(checkpoint, session)
    as_log = {**checkpoint.manifest, "kind": "log", "ref": "tr-0123456789ab", "bytes": 3, "stored": 3}
    other = replace(checkpoint, manifest=as_log, sig=runlog.sign_manifest(device.identity.sig, as_log))
    assert error(checkpoints.commit(other, extensions=NOT_CONTRACT)) == (400, {"error": "header_invalid"})
    # no or another device's signature, a malformed ref, a manifest that fits neither the chunks nor the task
    unsigned = replace(log, sig=b64u_encode(bytes(64)))
    assert error(checkpoints.commit(unsigned)) == (401, {"error": "signature_invalid"})
    assert error(checkpoints.commit(log.resigned(checkpoints.scene.other_device))) == (
        401,
        {"error": "signature_invalid"},
    )
    wrong_ref = replace(log, manifest={**log.manifest, "ref": "x-0123456789ab"})
    assert error(checkpoints.commit(wrong_ref, extensions=NOT_CONTRACT)) == (400, {"error": "header_invalid"})
    assert error(checkpoints.commit(log.resigned(device, size=int(str(log.manifest["size"])) + 1))) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/manifest/size"]}},
    )
    assert error(checkpoints.commit(log.resigned(device, task=new_id()))) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/manifest/task"]}},
    )
    assert checkpoints.commit(log).status_code == 200
    assert checkpoints.commit(checkpoint).status_code == 200


def test_logs_and_checkpoints_are_pruned_each_on_their_own(checkpoints: Checkpoints, limits: Limits) -> None:
    limits(context_keep=1, log_keep=2)
    desk, clock = checkpoints.outbox.desk, checkpoints.outbox.hub.clock
    task, session = checkpoints.outbox.session()
    other_task, other_session = checkpoints.outbox.session()
    foreign = checkpoints.logged(other_task, other_session)
    pending = checkpoints.seal_log(task, session)
    checkpoints.put(pending.object_id, 0, session, pending.chunks[0], kind="log")
    saved = checkpoints.saved(task, session)
    logs = []
    for _ in range(4):
        clock.advance(1)
        logs.append(checkpoints.logged(task, session))
    # the newest two complete logs of the task stay; the checkpoint, another task's log and the upload are untouched
    assert set(kinds(checkpoints)) == {
        foreign.object_id,
        pending.object_id,
        saved.object_id,
        logs[2].object_id,
        logs[3].object_id,
    }
    pruned = [row[3]["pruned"] for row in audit_rows(desk, "session.context_saved")]
    assert pruned == [0, 0, 0, 0, 1, 1]
    for gone in logs[:2]:
        assert error(checkpoints.scene.reader.browser.get(f"/v1/tasks/{task}/logs/{gone.object_id}")) == NOT_FOUND
    # a further checkpoint prunes only checkpoints: every log stays
    clock.advance(1)
    newer = checkpoints.saved(task, session)
    assert set(kinds(checkpoints)) == {
        foreign.object_id,
        pending.object_id,
        newer.object_id,
        logs[2].object_id,
        logs[3].object_id,
    }
    # the order is the creation, never the commit: the oldest log committed last stays until the next commit
    checkpoints.upload(pending, session)
    clock.advance(1)
    assert checkpoints.commit(pending).status_code == 200
    assert {logs[2].object_id, logs[3].object_id, pending.object_id} <= set(kinds(checkpoints))
    clock.advance(1)
    last = checkpoints.logged(task, session)
    assert set(kinds(checkpoints)) == {foreign.object_id, newer.object_id, logs[3].object_id, last.object_id}
    assert [row[3]["pruned"] for row in audit_rows(desk, "session.context_saved")][-2:] == [0, 2]
