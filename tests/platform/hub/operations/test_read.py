"""U-HUB-operations: the browser reads `GET /v1/tasks/{task}/operations` and `GET /v1/operations/{id}` (03-api.md
section 4.7): order, pages, rights (404 for foreign and unknown IDs), the read log and the contracts."""

from __future__ import annotations

from tests.platform.hub.operations.support import NOT_CONTRACT, Ops
from tests.platform.hub.orgs.support import new_id


def test_list_newest_first_with_pages(ops: Ops) -> None:
    task, session = ops.session()
    created = []
    for _ in range(3):
        ops.hub.clock.advance(1)
        created.append(ops.decide(session)[0])
    _other_task, other = ops.session()
    ops.decide(other)
    reader = ops.scene.reader.browser
    first = reader.get(f"/v1/tasks/{task}/operations", params={"limit": "2"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert [row["operation_id"] for row in body["operations"]] == created[::-1][:2]
    assert body["next_before"] is not None
    rest = reader.get(f"/v1/tasks/{task}/operations", params={"limit": "2", "before": body["next_before"]}).json()
    assert [row["operation_id"] for row in rest["operations"]] == [created[0]]
    assert rest["next_before"] is None
    assert len(reader.get(f"/v1/tasks/{task}/operations").json()["operations"]) == 3
    ops.hub.flush_read_log()
    assert ops.desk.rows("SELECT count(*) FROM hub.audit WHERE action = 'read.task.operations'") == [(3,)]


def test_list_refusals(ops: Ops) -> None:
    task, _session = ops.session()
    reader = ops.scene.reader.browser
    assert reader.get(f"/v1/tasks/{new_id()}/operations").json() == {"error": "not_found"}
    assert reader.get("/v1/tasks/not-an-id/operations").json() == {"error": "not_found"}
    assert ops.scene.stranger.browser.get(f"/v1/tasks/{task}/operations").json() == {"error": "not_found"}
    response = reader.get(f"/v1/tasks/{task}/operations", params={"limit": "0"}, extensions=NOT_CONTRACT)
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")
    response = reader.get(f"/v1/tasks/{task}/operations", params={"before": "nope"})
    assert (response.status_code, response.json()["error"]) == (400, "request_invalid")


def test_get_operation_with_transitions(ops: Ops) -> None:
    _task, session = ops.session()
    operation, _answer = ops.decide(session, dedupe="g")
    result = ops.finish(operation)
    response = ops.scene.reader.browser.get(f"/v1/operations/{operation}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operation"]["operation_id"] == operation
    assert body["operation"]["state"] == "succeeded"
    assert body["operation"]["result_event_id"] == result
    assert body["operation"]["params_event_id"] is not None
    assert [(row["seq"], row["state"], row["by_id"]) for row in body["transitions"]] == [
        (1, "received", ops.device.id),
        (2, "accepted", None),
        (3, "executing", ops.device.id),
        (4, "succeeded", ops.device.id),
    ]
    ops.hub.flush_read_log()
    assert ops.desk.rows("SELECT target_id FROM hub.audit WHERE action = 'read.operation'") != []


def test_get_operation_refusals(ops: Ops) -> None:
    _task, session = ops.session()
    operation, _answer = ops.decide(session)
    assert ops.scene.reader.browser.get(f"/v1/operations/{new_id()}").json() == {"error": "not_found"}
    assert ops.scene.reader.browser.get("/v1/operations/x").json() == {"error": "not_found"}
    assert ops.scene.stranger.browser.get(f"/v1/operations/{operation}").json() == {"error": "not_found"}
