"""U-HUB-tasks (ADR-076 section 3): `POST /v1/tasks` with `github_identity`. The identity must be usable by the
creator (own, or a company record of an org the creator is a member of), passes the feature gate of the org, is
stored on the task and returned in the task row; a repeat must name the same identity."""

from __future__ import annotations

import secrets
from typing import Any

from agent_hub.crypto.ids import uuid7
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import Desk


def _identity(desk: Desk, owner: str, owner_kind: str = "user") -> str:
    record_id = uuid7()
    now = desk.hub.clock()
    desk.execute(
        "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at, owner_kind,"
        " created_at, is_primary) VALUES (%s, %s, 'github', 1, 1, '{}', %s, %s, %s, %s, %s, false)",
        record_id,
        owner,
        secrets.token_bytes(28),
        secrets.token_bytes(64),
        now,
        owner_kind,
        now,
    )
    return record_id


def _announce(desk: Desk, *devices: Actor) -> None:
    for device in devices:
        desk.execute("UPDATE hub.device SET features = '{github.identities}' WHERE id = %s", device.id)


def _create(desk: Desk, actor: Actor, identity: str, task: str | None = None, **options: Any) -> Any:
    task_id = task or new_id()
    body = {
        "task_id": task_id,
        "wg_id": desk.wg,
        "events": desk.task_events(actor, task_id),
        "github_identity": identity,
    }
    return actor.browser.post("/v1/tasks", json=body, **options), body


def test_a_task_is_created_with_an_identity_its_creator_may_use(desk: Desk) -> None:
    writer = desk.scene.writer
    _announce(desk, desk.scene.device, desk.scene.other_device)
    for identity in (_identity(desk, writer.id), _identity(desk, desk.org, "org")):
        response, body = _create(desk, writer, identity)
        assert response.status_code == 200, response.text
        assert response.json()["task"]["github_identity"] == identity
        assert desk.rows("SELECT github_identity::text FROM hub.task WHERE id = %s", body["task_id"]) == [(identity,)]
        replayed = writer.browser.post("/v1/tasks", json=body)
        assert replayed.json()["replayed"] is True
        other = writer.browser.post("/v1/tasks", json={**body, "github_identity": _identity(desk, writer.id)})
        assert (other.status_code, other.json()) == (409, {"error": "id_conflict"})
    plain = desk.create(writer)
    assert plain.json()["task"]["github_identity"] is None


def test_another_persons_identity_and_an_outdated_device_are_refused(desk: Desk) -> None:
    writer = desk.scene.writer
    _announce(desk, desk.scene.device)
    for identity in (_identity(desk, desk.scene.reader.id), _identity(desk, desk.scene.stranger.id, "org"), new_id()):
        response, _body = _create(desk, writer, identity)
        assert (response.status_code, response.json()) == (403, {"error": "identity_forbidden"})
    response, _body = _create(desk, writer, _identity(desk, writer.id))
    assert (response.status_code, response.json()) == (
        409,
        {"error": "peer_outdated", "detail": {"devices": [desk.scene.other_device.id]}},
    )
    response, _body = _create(desk, writer, "not-a-uuid", extensions={"contract": False})
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/github_identity"]}}
    assert desk.rows("SELECT count(*) FROM hub.task") == [(0,)]


def test_a_task_takes_a_placement_and_an_identity_and_a_repeat_must_name_both(desk: Desk) -> None:
    writer, other = desk.scene.writer, desk.scene.other_device
    _announce(desk, desk.scene.device, other)
    identity = _identity(desk, writer.id)
    task_id = new_id()
    body = {
        "task_id": task_id,
        "wg_id": desk.wg,
        "events": desk.task_events(writer, task_id),
        "github_identity": identity,
        "placement": {"device_id": other.id},
    }
    response = writer.browser.post("/v1/tasks", json=body)
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert (task["github_identity"], task["placement"]) == (identity, {"device_id": other.id, "vm_id": None})
    assert writer.browser.post("/v1/tasks", json=body).json()["replayed"] is True
    without = {key: value for key, value in body.items() if key != "placement"}
    assert writer.browser.post("/v1/tasks", json=without).json()["replayed"] is True  # the stored choice is kept
    for changed in (
        {**body, "placement": {"device_id": desk.scene.device.id}},
        {**body, "placement": {"device_id": other.id, "vm_id": "vm-0000abcd"}},
        {key: value for key, value in body.items() if key != "github_identity"},
    ):
        conflict = writer.browser.post("/v1/tasks", json=changed)
        assert (conflict.status_code, conflict.json()) == (409, {"error": "id_conflict"})
