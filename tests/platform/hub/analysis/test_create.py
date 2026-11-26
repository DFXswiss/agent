"""U-HUB-analysis (create): `POST /v1/tasks` with `analysis` (ADR-095 sections 1 and 2). The task stores its scope
and period, gets the snapshot of the built-in profile `analysis` in the creating transaction (reach `all` for scope
`org`, `workgroup` for scope `wg`), carries `analysis` in its row and in the audit detail of `task.create`, passes the
feature gate `agent.profiles` and `platform.analysis` of every device of the organization, and a repetition must name
the same scope and the same explicit times."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.device.features import AGENT_PROFILES, PLATFORM_ANALYSIS
from agent_hub.profiles.model import BUILTIN
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import Desk, audit_rows

NOT_CONTRACT = {"contract": False}
SINCE, UNTIL = "2026-11-01T00:00:00.000Z", "2026-11-21T12:00:00.000Z"


def _announce(desk: Desk, features: list[str], *devices: Actor) -> None:
    for device in devices or (desk.scene.device, desk.scene.other_device):
        desk.execute("UPDATE hub.device SET features = %s WHERE id = %s", features, device.id)


def _body(desk: Desk, analysis: Any, task: str | None = None) -> dict[str, Any]:
    task_id = task or new_id()
    writer = desk.scene.writer
    return {"task_id": task_id, "wg_id": desk.wg, "events": desk.task_events(writer, task_id), "analysis": analysis}


def _post(desk: Desk, body: dict[str, Any], **options: Any) -> Any:
    return desk.scene.writer.browser.post("/v1/tasks", json=body, **options)


def _snapshot(reads: str) -> dict[str, Any]:
    return {**BUILTIN["analysis"].as_json(), "reads": reads}


@pytest.mark.parametrize(("scope", "reads"), [("org", "all"), ("wg", "workgroup")])
def test_an_analysis_task_reads_only_with_the_reach_of_its_scope(desk: Desk, scope: str, reads: str) -> None:
    _announce(desk, [AGENT_PROFILES, PLATFORM_ANALYSIS])
    body = _body(desk, {"scope": scope})
    response = _post(desk, body)
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    stored = {"scope": scope, "since": "2026-11-14T12:00:00.123Z", "until": "2026-11-21T12:00:00.123Z"}
    assert task["analysis"] == stored
    assert task["agent_profile"] == _snapshot(reads)
    assert desk.rows("SELECT analysis, agent_profile FROM hub.task WHERE id = %s", body["task_id"]) == [
        (stored, _snapshot(reads))
    ]
    [row] = audit_rows(desk, "task.create")
    assert row[-1] == {"origin": "browser", "analysis": scope}
    assert desk.task(body["task_id"])["analysis"] == stored
    listed = desk.scene.writer.browser.get(f"/v1/workgroups/{desk.wg}/tasks").json()["tasks"]
    assert [item["analysis"] for item in listed] == [stored]


def test_an_ordinary_task_has_no_analysis_and_no_profile(desk: Desk) -> None:
    response = desk.create(desk.scene.writer)
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert (task["analysis"], task["agent_profile"]) == (None, None)
    [row] = audit_rows(desk, "task.create")
    assert row[-1] == {"origin": "browser"}


def test_a_repetition_names_the_same_scope_and_the_same_explicit_times(desk: Desk) -> None:
    _announce(desk, [AGENT_PROFILES, PLATFORM_ANALYSIS])
    body = _body(desk, {"scope": "wg", "since": SINCE, "until": UNTIL})
    first = _post(desk, body)
    assert first.status_code == 200, first.text
    assert first.json()["task"]["analysis"] == {"scope": "wg", "since": SINCE, "until": UNTIL}
    desk.hub.clock.advance(30)
    again = _post(desk, body)
    assert again.json()["replayed"] is True
    assert again.json()["task"] == first.json()["task"]
    named_since = _post(desk, {**body, "analysis": {"scope": "wg", "since": SINCE}})
    assert named_since.json()["replayed"] is True  # an omitted time is not compared (its default moves with time)
    for changed in (
        {"scope": "org", "since": SINCE, "until": UNTIL},
        {"scope": "wg", "since": "2026-11-02T00:00:00.000Z", "until": UNTIL},
        {"scope": "wg", "since": SINCE, "until": "2026-11-21T11:00:00.000Z"},
        None,
    ):
        conflict = _post(desk, {**body, "analysis": changed}, extensions=NOT_CONTRACT)
        assert (conflict.status_code, conflict.json()) == (409, {"error": "id_conflict"}), changed
    assert desk.rows("SELECT count(*) FROM hub.task") == [(1,)]


def test_a_late_retry_gets_the_stored_answer(desk: Desk) -> None:
    """The period is checked against the hub's time only for a new task: a retry after the first request, whose
    `since` is now more than the longest period before the hub's time, is still answered from the stored task."""
    _announce(desk, [AGENT_PROFILES, PLATFORM_ANALYSIS])
    early = "2026-08-21T12:00:01.123Z"  # one second less than the longest period before the hub's time
    body = _body(desk, {"scope": "org", "since": early})
    first = _post(desk, body)
    assert first.status_code == 200, first.text
    desk.hub.clock.advance(2)
    again = _post(desk, body)
    assert (again.status_code, again.json().get("replayed")) == (200, True), again.text
    assert again.json()["task"]["analysis"] == first.json()["task"]["analysis"]
    unknown = _post(desk, {**body, "analysis": {"scope": "org", "since": early, "days": 7}}, extensions=NOT_CONTRACT)
    assert (unknown.status_code, unknown.json()) == (409, {"error": "id_conflict"})
    fresh = _post(desk, _body(desk, {"scope": "org", "since": early}))
    assert (fresh.status_code, fresh.json()["detail"]) == (400, {"pointers": ["/analysis/since"]})


def test_an_ordinary_task_repeated_as_an_analysis_is_a_conflict(desk: Desk) -> None:
    _announce(desk, [AGENT_PROFILES, PLATFORM_ANALYSIS])
    task_id = new_id()
    events = desk.task_events(desk.scene.writer, task_id)
    assert desk.create(desk.scene.writer, task_id, events=events).status_code == 200
    body = {"task_id": task_id, "wg_id": desk.wg, "events": events, "analysis": {"scope": "org"}}
    conflict = _post(desk, body)
    assert (conflict.status_code, conflict.json()) == (409, {"error": "id_conflict"})


def test_the_feature_gate_names_every_outdated_device(desk: Desk) -> None:
    device, other = desk.scene.device, desk.scene.other_device
    both = sorted([device.id, other.id])
    for features, devices, outdated in (
        ([], (), both),
        ([AGENT_PROFILES], (), both),
        ([PLATFORM_ANALYSIS], (), both),
        ([AGENT_PROFILES, PLATFORM_ANALYSIS], (device,), [other.id]),
    ):
        _announce(desk, [], device, other)
        _announce(desk, features, *devices)
        response = _post(desk, _body(desk, {"scope": "org"}))
        assert (response.status_code, response.json()) == (
            409,
            {"error": "peer_outdated", "detail": {"devices": outdated}},
        ), features
    assert desk.rows("SELECT count(*) FROM hub.task") == [(0,)]
    plain = desk.create(desk.scene.writer)  # an ordinary task needs neither feature
    assert plain.status_code == 200, plain.text


@pytest.mark.parametrize(
    ("analysis", "pointer"),
    [
        ({"scope": "device"}, "/analysis/scope"),
        ({"since": SINCE}, "/analysis/scope"),
        ({"scope": "wg", "days": 7}, "/analysis/days"),
        ({"scope": "wg", "until": "2026-11-21T12:01:00.124Z"}, "/analysis/until"),
        ({"scope": "wg", "since": "2026-11-21T12:00:00.123Z"}, "/analysis/since"),
        ({"scope": "wg", "since": "2026-08-01T00:00:00.000Z"}, "/analysis/since"),
        ({"scope": "wg", "since": "yesterday"}, "/analysis/since"),
    ],
)
def test_an_invalid_analysis_is_refused_with_its_pointer(desk: Desk, analysis: Any, pointer: str) -> None:
    _announce(desk, [AGENT_PROFILES, PLATFORM_ANALYSIS])
    response = _post(desk, _body(desk, analysis), extensions=NOT_CONTRACT)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": [pointer]}},
    )
    assert desk.rows("SELECT count(*) FROM hub.task") == [(0,)]


def test_a_reader_creates_no_analysis(desk: Desk) -> None:
    _announce(desk, [AGENT_PROFILES, PLATFORM_ANALYSIS])
    reader = desk.scene.reader
    task_id = new_id()
    events = desk.task_events(reader, task_id)
    body = {"task_id": task_id, "wg_id": desk.wg, "events": events, "analysis": {"scope": "wg"}}
    assert reader.browser.post("/v1/tasks", json=body).status_code == 403
    assert desk.rows("SELECT count(*) FROM hub.task") == [(0,)]
