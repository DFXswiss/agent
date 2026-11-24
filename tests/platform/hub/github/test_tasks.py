"""U-HUB-github (ADR-076 sections 2 and 3): the identity of a task (`PUT /v1/tasks/{task}/github-identity`). Writers
of the workgroup set it to an identity they may use (own, or a company record of an org they are a member of; never
another person's: `identity_forbidden`), clearing is always allowed, the feature gate refuses a choice while a device
of the org runs an older runner; the task row, the `task` frame and the device state carry it."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.github.support import NO_CONTRACT, Studio
from tests.platform.hub.orgs.support import new_id


def _put(caller: Any, task: str, identity: str | None, **options: Any) -> Any:
    return caller.browser.put(f"/v1/tasks/{task}/github-identity", json={"identity_id": identity}, **options)


def test_a_writer_chooses_and_clears_the_task_identity(studio: Studio, published: MemoryPublisher) -> None:
    writer, device = studio.scene.writer, studio.scene.device
    studio.announce()
    task, session = studio.scene.start_session()
    own = studio.created(writer)["header"]["id"]
    published.clear()
    response = _put(writer, task, own)
    assert (response.status_code, response.json()) == (200, {})
    assert studio.rows("SELECT github_identity::text FROM hub.task WHERE id = %s", task) == [(own,)]
    [(actor_kind, actor_id, _device, target_kind, target_id, detail)] = studio.audit("task.github_identity")
    assert (actor_kind, str(actor_id), target_kind, target_id, detail) == (
        "user",
        writer.id,
        "task",
        task,
        {"identity_id": own},
    )
    frames = [dict(item.data) for item in published.items() if item.frame == "task"]
    assert [frame["github_identity"] for frame in frames] == [own]
    reconciles = [str(item.audience.id) for item in published.items() if item.frame == "reconcile"]
    assert reconciles == [device.id]
    assert writer.browser.get(f"/v1/tasks/{task}").json()["task"]["github_identity"] == own
    state = studio.desk.client(device).get("/v1/device/state").json()
    [item] = [item for item in state["sessions"] if item["session_id"] == session]
    assert (item["task_github_identity"], item["github_identity"]) == (own, None)
    published.clear()
    assert _put(writer, task, own).status_code == 200  # unchanged
    assert len(studio.audit("task.github_identity")) == 1
    assert list(published.items()) == []
    company = studio.created_company()["header"]["id"]
    assert _put(writer, task, company).status_code == 200
    assert _put(writer, task, None).status_code == 200
    assert studio.rows("SELECT github_identity FROM hub.task WHERE id = %s", task) == [(None,)]
    assert [row[5] for row in studio.audit("task.github_identity")][1:] == [
        {"identity_id": company},
        {"identity_id": None},
    ]


def test_another_persons_identity_is_forbidden(studio: Studio) -> None:
    writer, reader, stranger = studio.scene.writer, studio.scene.reader, studio.scene.stranger
    studio.announce()
    task, _session = studio.scene.start_session()
    for identity in (
        studio.created(reader)["header"]["id"],
        studio.raw_identity(stranger.id, owner_kind="org"),
        new_id(),
    ):
        response = _put(writer, task, identity)
        assert (response.status_code, response.json()) == (403, {"error": "identity_forbidden"})
    assert studio.audit("task.github_identity") == []


def test_the_feature_gate_refuses_a_task_choice(studio: Studio) -> None:
    writer, other = studio.scene.writer, studio.scene.other_device
    studio.announce(studio.scene.device)
    task, _session = studio.scene.start_session()
    own = studio.created(writer)["header"]["id"]
    response = _put(writer, task, own)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "peer_outdated", "detail": {"devices": [other.id]}},
    )
    assert _put(writer, task, None).status_code == 200


@pytest.mark.parametrize("case", ["reader", "unknown", "bad_id"])
def test_refused_task_writes(studio: Studio, case: str) -> None:
    writer, reader = studio.scene.writer, studio.scene.reader
    task, _session = studio.scene.start_session()
    caller, path = {
        "reader": (reader, task),
        "unknown": (writer, new_id()),
        "bad_id": (writer, "not-a-uuid"),
    }[case]
    response = _put(caller, path, None, extensions=NO_CONTRACT)
    expected = (403, "forbidden") if case == "reader" else (404, "not_found")
    assert (response.status_code, response.json()) == (expected[0], {"error": expected[1]})
