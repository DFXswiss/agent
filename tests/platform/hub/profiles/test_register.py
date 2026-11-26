"""U-HUB-profiles: check H2b of the registration (ADR-093 sections 1 and 3) - no profile keeps every right (A1, E-11),
a profile limits the types of a session, a child is started with a profile within its parent's and never more, a
child without one has its parent's rights, a restriction on the task reaches children started before it, and the
section `agent_profiles` of the device state."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from agent_hub.operations.types import OPERATION_TYPES
from agent_hub.profiles.model import BUILTIN
from tests.platform.hub.operations.support import Ops

SUBAGENT = "subagent.request"


def _task_profile(ops: Ops, task: str, name: str | None) -> None:
    snapshot = None if name is None else Jsonb(BUILTIN[name].as_json())
    ops.desk.execute("UPDATE hub.task SET agent_profile = %s WHERE id = %s", snapshot, task)


def _child(ops: Ops, session: str, profile: str | None = None, **options: Any) -> dict[str, Any]:
    body = ops.request(session, SUBAGENT, **options)
    if profile is not None:
        body["child_profile"] = profile
    response = ops.register(body)
    assert response.status_code == 200, response.text
    answer = dict(response.json())
    if "child_session_id" in answer:
        ops.scene.sessions[answer["child_session_id"]] = ops.scene.sessions[session]
    return answer


def test_without_a_profile_every_type_is_accepted(ops: Ops) -> None:
    _task, session = ops.session()
    for op_type in sorted(OPERATION_TYPES - {SUBAGENT, "human.question", "human.approval"}):
        _operation, answer = ops.decide(session, op_type)
        assert answer == {"state": "accepted"}, op_type
    child = _child(ops, session)
    assert child["state"] == "accepted"
    assert "child_profile" not in child
    _operation, answer = ops.decide(child["child_session_id"], "github.push")
    assert answer == {"state": "accepted"}


def test_a_task_profile_limits_its_sessions(ops: Ops) -> None:
    task, session = ops.session()
    _task_profile(ops, task, "read_only")
    operation, answer = ops.decide(session, "github.comment")
    assert answer == {"state": "denied", "error_code": "rights_denied"}
    assert ops.audit(operation)[0][:2] == ("operation.denied", "denied")
    assert ops.decide(session, "github.get_pull_request")[1] == {"state": "accepted"}
    # a local rejection stays the runner's
    assert ops.decide(session, "github.comment", local_check="rejected", error_code="params_invalid")[1] == {
        "state": "rejected",
        "error_code": "params_invalid",
    }


def test_a_child_inherits_and_never_exceeds_its_parent(ops: Ops) -> None:
    task, session = ops.session()
    _task_profile(ops, task, "development")
    narrower = _child(ops, session, "read_only")
    child = narrower["child_session_id"]
    assert narrower["child_profile"] == BUILTIN["read_only"].as_json()
    assert ops.desk.rows("SELECT agent_profile FROM hub.session WHERE id = %s", child) == [
        (BUILTIN["read_only"].as_json(),)
    ]
    assert ops.decide(child, "github.commit")[1] == {"state": "denied", "error_code": "rights_denied"}
    # a grandchild cannot regain what its parent lost, with or without a profile of its own
    assert _child(ops, child, "development") == {"state": "denied", "error_code": "rights_denied"}
    inheriting = _child(ops, child)
    assert inheriting["state"] == "accepted"
    assert "child_profile" not in inheriting
    grandchild = inheriting["child_session_id"]
    assert ops.desk.rows("SELECT agent_profile FROM hub.session WHERE id = %s", grandchild) == [(None,)]
    assert ops.decide(grandchild, "github.push")[1] == {"state": "denied", "error_code": "rights_denied"}
    assert ops.decide(grandchild, "github.get_issue")[1] == {"state": "accepted"}
    assert _child(ops, session, "unknown") == {"state": "denied", "error_code": "rights_denied"}


def test_narrowing_the_task_reaches_a_child_started_before(ops: Ops) -> None:
    task, session = ops.session()
    child = _child(ops, session)["child_session_id"]
    assert ops.decide(child, "github.push")[1] == {"state": "accepted"}
    _task_profile(ops, task, "read_only")
    assert ops.decide(child, "github.push")[1] == {"state": "denied", "error_code": "rights_denied"}
    assert ops.decide(session, SUBAGENT)[1]["state"] == "accepted"


def test_a_replay_answers_the_child_profile_again(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session, SUBAGENT)
    body["child_profile"] = "merge"
    first = ops.register(body).json()
    again = ops.register(body).json()
    assert again == {**first, "replayed": True}
    assert first["child_profile"] == BUILTIN["merge"].as_json()


def test_the_device_state_names_the_effective_profiles(ops: Ops) -> None:
    task, session = ops.session()
    other_task, other = ops.session()
    _task_profile(ops, task, "review")
    child = _child(ops, session, "read_only")["child_session_id"]
    state = ops.fleet.client(ops.device).get("/v1/device/state").json()
    section = state["agent_profiles"]
    assert section["api"] == 1
    read_only = BUILTIN["read_only"].operations & BUILTIN["review"].operations
    assert section["sessions"][session] == BUILTIN["review"].as_json()
    assert section["sessions"][child] == {"name": "read_only", "operations": sorted(read_only), "reads": "all"}
    assert section["sessions"][other] is None
    assert sorted(section["workgroups"][ops.desk.wg]) == sorted(BUILTIN)
    assert other_task != task
