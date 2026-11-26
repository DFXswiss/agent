"""U-HUB-profiles: own profiles of a workgroup or an organization, the listing with the built-in roles and the profile
of a task (ADR-093 sections 1, 2 and 7; `PUT/DELETE /v1/workgroups/{wg}/agent-profiles/{name}`,
`PUT/DELETE /v1/orgs/{org}/agent-profiles/{name}`, `GET /v1/workgroups/{wg}/agent-profiles`,
`PUT /v1/tasks/{task}/agent-profile`)."""

from __future__ import annotations

from typing import Any

from agent_hub.device.features import AGENT_PROFILES
from agent_hub.profiles.model import BUILTIN, CLASSES
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import audit_rows, frames

READER = {"operations": ["github.get_issue", "github.list_issues"], "reads": "task"}


def _wg(ops: Ops, actor: Actor, name: str, body: dict[str, Any] | None = READER, **options: Any) -> Any:
    path = f"/v1/workgroups/{ops.desk.wg}/agent-profiles/{name}"
    if body is None:
        return actor.browser.delete(path, **options)
    return actor.browser.put(path, json=body, **options)


def _org(ops: Ops, actor: Actor, name: str, body: dict[str, Any] | None = READER, **options: Any) -> Any:
    path = f"/v1/orgs/{ops.desk.org}/agent-profiles/{name}"
    if body is None:
        return actor.browser.delete(path, **options)
    return actor.browser.put(path, json=body, **options)


def _listing(ops: Ops, actor: Actor) -> Any:
    return actor.browser.get(f"/v1/workgroups/{ops.desk.wg}/agent-profiles")


def _announce(ops: Ops, *features: str) -> None:
    for device in (ops.scene.device, ops.scene.other_device):
        ops.desk.execute("UPDATE hub.device SET features = %s WHERE id = %s", list(features), device.id)


def _reconciles(published: MemoryPublisher) -> list[str]:
    return sorted(str(item.audience.id) for item in published.items() if item.frame == "reconcile")


def test_the_listing_names_the_roles_and_the_classes(ops: Ops) -> None:
    response = _listing(ops, ops.scene.reader)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["classes"] == dict(CLASSES)
    assert [item["name"] for item in body["profiles"]] == sorted(BUILTIN)
    assert all(item["scope"] == "builtin" and item["updated_at"] is None for item in body["profiles"])
    assert _listing(ops, ops.scene.stranger).status_code == 404


def test_a_writer_defines_a_workgroup_profile(ops: Ops, published: MemoryPublisher) -> None:
    writer = ops.scene.writer
    published.clear()
    assert (_wg(ops, writer, "triage").status_code, _wg(ops, writer, "triage").json()) == (200, {})
    assert _reconciles(published) == sorted([ops.scene.device.id, ops.scene.other_device.id])
    [row] = audit_rows(ops.desk, "agent_profile.set")
    assert row[-1] == {"scope": "wg", "profile": "triage", "reads": "task", "operations": 2}
    listed = {item["name"]: item for item in _listing(ops, writer).json()["profiles"]}
    assert listed["triage"]["scope"] == "wg"
    assert listed["triage"]["reads"] == "task"
    assert listed["triage"]["operations"] == READER["operations"]
    changed = {"operations": ["github.get_issue"], "reads": "workgroup"}
    assert _wg(ops, writer, "triage", changed).status_code == 200
    assert len(audit_rows(ops.desk, "agent_profile.set")) == 2
    assert _wg(ops, ops.scene.reader, "triage").status_code == 403
    assert _wg(ops, writer, "triage", None).status_code == 200
    assert [row[-1] for row in audit_rows(ops.desk, "agent_profile.delete")] == [{"scope": "wg", "profile": "triage"}]
    assert _wg(ops, writer, "triage", None).status_code == 404
    assert "triage" not in {item["name"] for item in _listing(ops, writer).json()["profiles"]}


def test_definitions_are_checked(ops: Ops) -> None:
    writer = ops.scene.writer
    bad = {"operations": ["github.get_issue", "nope.thing"], "reads": "all"}
    response = _wg(ops, writer, "x", bad)
    assert (response.status_code, response.json()["detail"]) == (400, {"pointers": ["/operations/1"]})
    response = _wg(ops, writer, "review")
    assert (response.status_code, response.json()["detail"]) == (400, {"pointers": ["/name"]})
    assert _wg(ops, writer, "Bad-Name").status_code == 404
    assert _wg(ops, writer, "Bad-Name", None).status_code == 404
    assert audit_rows(ops.desk, "agent_profile.set") == []


def test_admins_define_organization_profiles_and_workgroups_win(ops: Ops) -> None:
    admin, writer = ops.scene.admin, ops.scene.writer
    assert _org(ops, writer, "auditor").status_code == 403
    assert _org(ops, admin, "auditor").status_code == 200
    assert _org(ops, admin, "auditor").status_code == 200  # unchanged
    assert len(audit_rows(ops.desk, "agent_profile.set")) == 1
    wide = {"operations": ["github.get_issue", "github.list_issues", "github.list_repositories"], "reads": "all"}
    assert _wg(ops, writer, "auditor", wide).status_code == 200
    listed = {item["name"]: item for item in _listing(ops, writer).json()["profiles"]}
    assert listed["auditor"]["scope"] == "wg"
    assert len(listed["auditor"]["operations"]) == 3
    assert _org(ops, admin, "auditor", None).status_code == 200
    assert _org(ops, admin, "auditor", None).status_code == 404
    assert _org(ops, writer, "auditor", None).status_code == 403


def test_a_writer_assigns_and_clears_the_task_profile(ops: Ops, published: MemoryPublisher) -> None:
    _announce(ops, AGENT_PROFILES)
    task, session = ops.session()
    writer = ops.scene.writer
    published.clear()
    response = writer.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": "read_only"})
    assert response.status_code == 200, response.text
    snapshot = BUILTIN["read_only"].as_json()
    assert response.json() == {"profile": snapshot}
    assert [frame["agent_profile"] for frame in frames(published, "task")] == [snapshot]
    assert _reconciles(published) == [ops.scene.device.id]
    [row] = audit_rows(ops.desk, "task.agent_profile")
    assert row[-1] == {"profile": "read_only", "reads": "all"}
    assert writer.browser.get(f"/v1/tasks/{task}").json()["task"]["agent_profile"] == snapshot
    published.clear()
    assert writer.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": "read_only"}).status_code == 200
    assert list(published.items()) == []
    assert len(audit_rows(ops.desk, "task.agent_profile")) == 1
    cleared = writer.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": None})
    assert cleared.json() == {"profile": None}
    assert audit_rows(ops.desk, "task.agent_profile")[-1][-1] == {"profile": None, "reads": None}
    assert session in ops.scene.sessions


def test_task_profile_refusals(ops: Ops) -> None:
    task, _session = ops.session()
    writer, reader = ops.scene.writer, ops.scene.reader
    unknown = writer.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": "nobody"})
    assert unknown.status_code == 404
    outdated = writer.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": "review"})
    assert outdated.status_code == 409
    assert outdated.json()["error"] == "peer_outdated"
    assert sorted(outdated.json()["detail"]["devices"]) == sorted([ops.scene.device.id, ops.scene.other_device.id])
    # clearing needs no feature: it only widens back to the default
    assert writer.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": None}).status_code == 200
    _announce(ops, AGENT_PROFILES)
    assert reader.browser.put(f"/v1/tasks/{task}/agent-profile", json={"profile": "review"}).status_code == 403
    assert writer.browser.put(f"/v1/tasks/{new_id()}/agent-profile", json={"profile": "review"}).status_code == 404
    assert audit_rows(ops.desk, "task.agent_profile") == []
