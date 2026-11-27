"""U-HUB-attachments: files and images attached to prompts as sealed objects of a workgroup - upload, commit, the
members' and the devices' reads, the quota and the removal of abandoned uploads (ADR-102 section 3; 03-api.md sections
4.6 and 6.1; 04-crypto.md section 4.14)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from agent_hub.attachments import jobs, store
from agent_hub.crypto import attachment
from agent_hub.crypto.encoding import b64u_decode, b64u_encode, format_time
from agent_hub.crypto.ids import uuid7
from tests.platform.hub.agents.support import agent_client
from tests.platform.hub.contexts.support import NOT_CONTRACT, WK, Limits, error
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.outbox.support import Outbox
from tests.platform.hub.tasks.support import audit_rows

NOT_FOUND = (404, {"error": "not_found"})
FORBIDDEN = (403, {"error": "forbidden"})
CONFLICT = (409, {"error": "id_conflict"})


@dataclass
class File:
    """One sealed attachment ready to upload."""

    object_id: str
    ref: dict[str, object]
    chunks: list[bytes]

    @property
    def values(self) -> dict[str, Any]:
        """The body of the commit."""
        return {
            "chunks": len(self.chunks),
            "size": sum(len(chunk) for chunk in self.chunks),
            "digest": self.ref["digest"],
        }


def seal(data: bytes = b"\x89PNG screenshot") -> File:
    object_id = uuid7()
    ref, chunks = attachment.seal_attachment(
        WK, object_id=object_id, name="shot.png", media_type="image/png", epoch=1, data=data, salt=os.urandom(32)
    )
    return File(object_id, ref, chunks)


def path(wg: str, object_id: str, tail: str) -> str:
    return f"/v1/workgroups/{wg}/attachments/{object_id}/{tail}"


def put(
    client: Any, wg: str, file: File, index: int | str, ct: bytes | str | None = None, **options: Any
) -> httpx.Response:
    body = b64u_encode(file.chunks[int(index)]) if ct is None else (ct if isinstance(ct, str) else b64u_encode(ct))
    response: httpx.Response = client.put(path(wg, file.object_id, f"chunks/{index}"), json={"ct": body}, **options)
    return response


def commit(
    client: Any, wg: str, file: File, options: dict[str, Any] | None = None, **changes: object
) -> httpx.Response:
    response: httpx.Response = client.post(
        path(wg, file.object_id, "commit"), json={**file.values, **changes}, **(options or {})
    )
    return response


def upload(client: Any, wg: str, file: File) -> None:
    for index in range(len(file.chunks)):
        assert put(client, wg, file, index).status_code == 200
    response = commit(client, wg, file)
    assert response.status_code == 200, response.text


def second_workgroup(outbox: Outbox) -> str:
    """A further workgroup of the scene's org in which writer and reader both write and no device is assigned."""
    scene = outbox.scene
    wg = new_id()
    scene.draft.add("wg_create", scene.admin, wg=wg, epoch=1)
    scene.draft.add("grant", scene.admin, subject=scene.writer.id, wg=wg, access="write")
    scene.draft.add("grant", scene.admin, subject=scene.reader.id, wg=wg, access="write")
    response = scene.draft.post(
        scene.admin, scene.draft.wraps(scene.admin, "wg", wg, 1, [scene.admin, scene.writer, scene.reader])
    )
    assert response.status_code == 200, response.text
    return wg


def stored(outbox: Outbox) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in outbox.desk.rows("SELECT id, state FROM hub.attachment_object")}


def device_get(outbox: Outbox, file: File, index: int | str, device: Actor | None = None) -> httpx.Response:
    response: httpx.Response = outbox.fleet.client(device or outbox.device).get(
        f"/v1/device/attachments/{file.object_id}/chunks/{index}"
    )
    return response


# --- upload, commit, reading ------------------------------------------------------------------------------------------


def test_an_attachment_is_uploaded_committed_and_read_by_members_and_devices(outbox: Outbox) -> None:
    hub, scene, desk = outbox.hub, outbox.scene, outbox.desk
    data = bytes(range(256)) * (attachment.CHUNK_BYTES // 256) + b"tail"
    file = seal(data)
    assert len(file.chunks) == 2
    for index, replayed in ((1, False), (0, False), (0, True)):
        assert error(put(scene.writer.browser, scene.wg, file, index)) == (200, {"replayed": replayed})
    # an upload in progress is read by nobody
    assert error(scene.reader.browser.get(path(scene.wg, file.object_id, "chunks/0"))) == NOT_FOUND
    assert error(device_get(outbox, file, 0)) == NOT_FOUND
    hub.clock.advance(2)
    completed = format_time(hub.clock())
    assert error(commit(scene.writer.browser, scene.wg, file)) == (200, {"completed_at": completed, "replayed": False})
    hub.clock.advance(1)
    assert error(commit(scene.writer.browser, scene.wg, file)) == (200, {"completed_at": completed, "replayed": True})
    assert stored(outbox) == {file.object_id: "complete"}
    assert audit_rows(desk, "attachment.stored") == [
        ("user", UUID(scene.writer.id), file.object_id, {"chunks": 2, "size": file.values["size"]})
    ]
    # every member with `wg:read` - a browser or the user's agent - and every device of the workgroup reads the
    # chunks; the file opens
    agent = agent_client(hub, UUID(scene.reader.id))
    for client in (scene.writer.browser, scene.reader.browser, agent):
        chunks = [client.get(path(scene.wg, file.object_id, f"chunks/{index}")).json()["ct"] for index in range(2)]
        assert attachment.open_attachment(WK, file.ref, [b64u_decode(chunk) for chunk in chunks]) == data
    for device in (outbox.device, scene.other_device):
        assert error(device_get(outbox, file, 1, device)) == (200, {"ct": b64u_encode(file.chunks[1])})
    hub.flush_read_log()
    assert {str(row[1]) for row in audit_rows(desk, "read.workgroup.attachment.chunk")} == {
        scene.writer.id,
        scene.reader.id,
    }
    assert {str(row[1]) for row in audit_rows(desk, "read.device.attachment.chunk")} == {
        outbox.device.id,
        scene.other_device.id,
    }
    # a chunk the object does not have names nothing
    assert error(scene.reader.browser.get(path(scene.wg, file.object_id, "chunks/2"))) == NOT_FOUND
    assert error(device_get(outbox, file, 2)) == NOT_FOUND


def test_a_stored_chunk_and_a_committed_object_never_change(outbox: Outbox) -> None:
    scene = outbox.scene
    file, other = seal(), seal(b"other bytes")
    assert put(scene.writer.browser, scene.wg, file, 0).status_code == 200
    assert error(put(scene.writer.browser, scene.wg, file, 0, other.chunks[0])) == CONFLICT
    assert commit(scene.writer.browser, scene.wg, file).status_code == 200
    assert error(put(scene.writer.browser, scene.wg, file, 1, other.chunks[0])) == CONFLICT
    assert error(commit(scene.writer.browser, scene.wg, file, size=file.values["size"] + 1)) == CONFLICT
    assert error(commit(scene.writer.browser, scene.wg, file, chunks=2)) == CONFLICT
    assert error(commit(scene.writer.browser, scene.wg, file, digest=other.ref["digest"])) == CONFLICT
    # the identical chunk again is a replay, also after the commit
    assert error(put(scene.writer.browser, scene.wg, file, 0)) == (200, {"replayed": True})


def test_an_object_belongs_to_one_workgroup_and_one_uploader(outbox: Outbox) -> None:
    scene = outbox.scene
    wg = second_workgroup(outbox)
    file = seal()
    assert put(scene.writer.browser, wg, file, 0).status_code == 200
    # the same object ID in another workgroup or by another member is a conflict
    assert error(put(scene.writer.browser, scene.wg, file, 0)) == CONFLICT
    assert error(put(scene.reader.browser, wg, file, 0)) == CONFLICT
    # only the uploader commits, and only in its workgroup
    assert error(commit(scene.reader.browser, wg, file)) == FORBIDDEN
    assert error(commit(scene.writer.browser, scene.wg, file)) == NOT_FOUND
    assert error(commit(scene.writer.browser, wg, seal())) == NOT_FOUND
    assert commit(scene.writer.browser, wg, file).status_code == 200
    # a complete object of another workgroup is read neither through this one nor by a device not assigned to it
    assert error(scene.writer.browser.get(path(scene.wg, file.object_id, "chunks/0"))) == NOT_FOUND
    assert error(device_get(outbox, file, 0)) == NOT_FOUND
    assert scene.writer.browser.get(path(wg, file.object_id, "chunks/0")).status_code == 200
    assert error(device_get(outbox, seal(), 0)) == NOT_FOUND


def test_only_writers_upload_and_only_members_read(outbox: Outbox) -> None:
    scene = outbox.scene
    file = seal()
    # the reader holds `wg:read` only; a stranger knows the workgroup but is no member
    assert error(put(scene.reader.browser, scene.wg, file, 0)) == FORBIDDEN
    assert error(commit(scene.reader.browser, scene.wg, file)) == FORBIDDEN
    assert error(put(scene.stranger.browser, scene.wg, file, 0)) == FORBIDDEN
    assert error(put(scene.writer.browser, new_id(), file, 0)) == NOT_FOUND
    upload(scene.writer.browser, scene.wg, file)
    assert error(scene.stranger.browser.get(path(scene.wg, file.object_id, "chunks/0"))) == NOT_FOUND


@pytest.mark.parametrize("index", ["32", "01", "-1", "x", "100"])
def test_an_index_outside_the_chunks_names_nothing(outbox: Outbox, index: str) -> None:
    scene, file = outbox.scene, seal()
    assert error(put(scene.writer.browser, scene.wg, file, index, file.chunks[0])) == NOT_FOUND
    assert error(scene.writer.browser.get(path(scene.wg, file.object_id, f"chunks/{index}"))) == NOT_FOUND
    assert error(device_get(outbox, file, index)) == NOT_FOUND


def test_malformed_ids_name_nothing(outbox: Outbox) -> None:
    scene, file = outbox.scene, seal()
    response = scene.writer.browser.put(path(scene.wg, "not-an-id", "chunks/0"), json={"ct": "A" * 43})
    assert error(response) == NOT_FOUND
    assert error(scene.writer.browser.put(path("x", file.object_id, "chunks/0"), json={"ct": "A" * 43})) == NOT_FOUND
    assert outbox.fleet.client(outbox.device).get("/v1/device/attachments/x/chunks/0").status_code == 404


def test_a_chunk_has_the_size_of_a_sealed_piece(outbox: Outbox) -> None:
    scene, file = outbox.scene, seal()
    pointer = (400, {"error": "request_invalid", "detail": {"pointers": ["/ct"]}})
    short = bytes(attachment.MIN_CT_BYTES - 1)
    assert error(put(scene.writer.browser, scene.wg, file, 0, short, extensions=NOT_CONTRACT)) == pointer
    assert error(put(scene.writer.browser, scene.wg, file, 0, "AAAA=", extensions=NOT_CONTRACT)) == pointer
    too_large = bytes(attachment.CHUNK_CT_BYTES + 1)
    assert error(put(scene.writer.browser, scene.wg, file, 0, too_large, extensions=NOT_CONTRACT)) == (
        413,
        {"error": "size_exceeded"},
    )
    assert put(scene.writer.browser, scene.wg, file, 0, bytes(attachment.CHUNK_CT_BYTES)).status_code == 200


def test_the_commit_names_exactly_the_stored_chunks(outbox: Outbox) -> None:
    scene = outbox.scene
    file = seal(bytes(attachment.CHUNK_BYTES * 2 + 1))
    assert len(file.chunks) == 3
    for index in (0, 2):
        assert put(scene.writer.browser, scene.wg, file, index).status_code == 200

    def refused(**changes: object) -> tuple[int, Any]:
        found = error(commit(scene.writer.browser, scene.wg, file, None, **changes))
        return found[0], found[1]["detail"]["pointers"]

    # a gap in the indexes, a wrong count, size or digest
    assert refused(chunks=2, size=len(file.chunks[0]) + len(file.chunks[2])) == (400, ["/chunks"])
    assert put(scene.writer.browser, scene.wg, file, 1).status_code == 200
    assert refused(chunks=2) == (400, ["/chunks"])
    assert refused(size=file.values["size"] - 1) == (400, ["/size"])
    assert refused(digest=seal().ref["digest"]) == (400, ["/digest"])
    assert stored(outbox) == {file.object_id: "uploading"}
    assert commit(scene.writer.browser, scene.wg, file).status_code == 200
    # the body is checked before anything else
    assert commit(scene.writer.browser, scene.wg, file, {"extensions": NOT_CONTRACT}, chunks=33).status_code == 400
    assert commit(scene.writer.browser, scene.wg, file, {"extensions": NOT_CONTRACT}, digest="AAAA").status_code == 400


def test_the_organisation_quota_counts_every_attachment(outbox: Outbox, limits: Limits) -> None:
    scene = outbox.scene
    first, second = seal(), seal(b"second file")
    upload(scene.writer.browser, scene.wg, first)
    used = len(first.chunks[0])
    limits(attachment_org_bytes=used + len(second.chunks[0]) - 1)
    quota = {"scope": "org", "limit": used + len(second.chunks[0]) - 1, "used": used}
    assert error(put(scene.writer.browser, scene.wg, second, 0)) == (
        413,
        {"error": "quota_exceeded", "detail": {"quota": quota}},
    )
    # the refusal leaves no object behind
    assert stored(outbox) == {first.object_id: "complete"}
    limits(attachment_org_bytes=used + len(second.chunks[0]))
    assert put(scene.writer.browser, scene.wg, second, 0).status_code == 200


def test_abandoned_uploads_expire(outbox: Outbox, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="agent_hub.attachments")
    scene, clock = outbox.scene, outbox.hub.clock
    hub = SimpleNamespace(pool=outbox.hub.pool, clock=clock)
    (job,) = jobs.JOBS
    assert (job.name, job.interval_seconds, job.run) == ("attachments.expire", 600.0, jobs.expire)
    done, left, going_on = seal(), seal(), seal()
    upload(scene.writer.browser, scene.wg, done)
    assert put(scene.writer.browser, scene.wg, left, 0).status_code == 200
    clock.advance(store.STALE_UPLOAD.total_seconds() - 1)
    assert put(scene.writer.browser, scene.wg, going_on, 0).status_code == 200
    clock.advance(1)
    assert jobs.expire(hub) == 0
    assert caplog.messages == []
    clock.advance(1)
    assert jobs.expire(hub) == 1
    assert caplog.messages == ["attachments.expire removed=1"]
    assert set(stored(outbox)) == {done.object_id, going_on.object_id}
    # the abandoned object can be uploaded again from the start
    assert error(put(scene.writer.browser, scene.wg, left, 0)) == (200, {"replayed": False})
