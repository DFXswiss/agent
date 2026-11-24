"""U-HUB-operations (ADR-076 sections 2, 3 and 6): the GitHub identity of a registration (check H2a).

`github_identity` and `triggered_by` are stored on the operation, written into every `operation.*` audit detail and
returned in the operation row. A registration names only an identity the acting human may use (a member registering
an operation with another person's personal identity is denied: the forged-identity attack) and a `triggered_by` of
the task's org. The backstop for older runners denies a `github.*` registration without `github_identity` while a
choice or default applies. A `subagent.request` passes its identity (or its parent's) to the child session."""

from __future__ import annotations

import secrets
from typing import Any

from agent_hub.crypto.ids import uuid7
from agent_hub.operations import jobs
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import Actor


def _identity(ops: Ops, owner: str, owner_kind: str = "user") -> str:
    record_id = uuid7()
    now = ops.hub.clock()
    ops.desk.execute(
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


def _register(ops: Ops, session: str, op_type: str = "github.comment", **fields: Any) -> tuple[str, dict[str, Any]]:
    body = ops.request(session, op_type)
    body.update({name: value for name, value in fields.items() if value is not None})
    response = ops.register(body)
    assert response.status_code == 200, response.text
    return str(body["operation_id"]), dict(response.json())


def _announce(ops: Ops, *devices: Actor) -> None:
    for device in devices:
        ops.desk.execute("UPDATE hub.device SET features = '{github.identities}' WHERE id = %s", device.id)


def test_the_identity_and_the_trigger_are_stored_and_audited(ops: Ops) -> None:
    writer, reader = ops.scene.writer, ops.scene.reader
    _task, session = ops.session()
    own = _identity(ops, writer.id)
    operation, answer = _register(ops, session, github_identity=own, triggered_by=writer.id)
    assert answer == {"state": "accepted"}
    assert ops.desk.rows(
        "SELECT github_identity::text, triggered_by::text FROM hub.operation WHERE id = %s", operation
    ) == [(own, writer.id)]
    [(action, _result, _kind, _actor, detail)] = ops.audit(operation)
    assert (action, detail["github_identity"], detail["triggered_by"]) == ("operation.accepted", own, writer.id)
    contracts_registry().validate("audit/operation.accepted.json", detail)
    row = writer.browser.get(f"/v1/operations/{operation}").json()["operation"]
    assert (row["github_identity"], row["triggered_by"]) == (own, writer.id)
    ops.finish(operation)
    details = {item[0]: item[4] for item in ops.audit(operation) if item[0].startswith("operation.")}
    assert sorted(details) == ["operation.accepted", "operation.executing", "operation.succeeded"]
    assert {(item["github_identity"], item["triggered_by"]) for item in details.values()} == {(own, writer.id)}
    # the human who triggered it may use a company record of the task's org; the reader's own record as well
    company = _identity(ops, ops.desk.org, "org")
    assert _register(ops, session, github_identity=company, triggered_by=reader.id)[1] == {"state": "accepted"}
    assert _register(ops, session, github_identity=_identity(ops, reader.id), triggered_by=reader.id)[1] == {
        "state": "accepted"
    }
    # a registration that repeats the operation ID with another identity is another operation
    body = ops.request(session, "github.comment")
    body["github_identity"] = own
    assert ops.register(body).status_code == 200
    assert ops.register({**body, "github_identity": company}).json() == {"error": "id_conflict"}
    assert ops.register(body).json()["replayed"] is True


def test_a_forged_identity_is_denied(ops: Ops) -> None:
    writer, reader, stranger = ops.scene.writer, ops.scene.reader, ops.scene.stranger
    _task, session = ops.session()
    for fields in (
        {"github_identity": _identity(ops, reader.id)},  # another person's personal identity
        {"github_identity": _identity(ops, reader.id), "triggered_by": writer.id},
        {"github_identity": _identity(ops, writer.id), "triggered_by": reader.id},
        {"github_identity": _identity(ops, stranger.id, "org")},  # a company record of another org
        {"github_identity": uuid7()},  # no such record
        {"triggered_by": stranger.id},  # nobody of the task's org
    ):
        operation, answer = _register(ops, session, **fields)
        assert answer == {"state": "denied", "error_code": "rights_denied"}, fields
        [(action, _result, _kind, _actor, detail)] = ops.audit(operation)
        assert action == "operation.denied"
        assert detail["github_identity"] == fields.get("github_identity")


def test_the_backstop_denies_old_runners_while_a_choice_applies(ops: Ops) -> None:
    writer = ops.scene.writer
    task, session = ops.session()
    own = _identity(ops, writer.id)
    assert _register(ops, session)[1] == {"state": "accepted"}
    for set_choice, clear in (
        ("UPDATE hub.task SET github_identity = %s WHERE id = %s", "UPDATE hub.task SET github_identity = NULL"),
        ("UPDATE hub.session SET github_identity = %s WHERE id = %s", "UPDATE hub.session SET github_identity = NULL"),
    ):
        ops.desk.execute(set_choice, own, task if "task" in set_choice else session)
        assert _register(ops, session)[1] == {"state": "denied", "error_code": "rights_denied"}
        assert _register(ops, session, github_identity=own)[1] == {"state": "accepted"}
        for free in ("github.request_connection", "github.list_identities", "task.set_title"):
            assert _register(ops, session, free)[1] == {"state": "accepted"}, free
        ops.desk.execute(clear)
    company = _identity(ops, ops.desk.org, "org")
    for scope, scope_id in (("org", ops.desk.org), ("wg", ops.desk.wg)):
        ops.desk.execute(
            "INSERT INTO hub.github_default (scope, scope_id, identity_id, set_by, set_at)"
            " VALUES (%s, %s, %s, %s, now())",
            scope,
            scope_id,
            company,
            writer.id,
        )
        assert _register(ops, session)[1] == {"state": "denied", "error_code": "rights_denied"}
        ops.desk.execute("DELETE FROM hub.github_default")
    assert _register(ops, session)[1] == {"state": "accepted"}


def test_a_subagent_takes_its_identity_or_its_parents(ops: Ops) -> None:
    writer = ops.scene.writer
    _announce(ops, ops.scene.device, ops.scene.other_device)
    _task, session = ops.session()
    own = _identity(ops, writer.id)
    _operation, answer = _register(ops, session, "subagent.request", github_identity=own)
    child = answer["child_session_id"]
    assert ops.desk.rows("SELECT github_identity::text FROM hub.session WHERE id = %s", child) == [(own,)]
    ops.scene.sessions[child] = ops.scene.sessions[session]
    _operation, answer = _register(ops, child, "subagent.request")
    grandchild = answer["child_session_id"]
    assert ops.desk.rows("SELECT github_identity::text FROM hub.session WHERE id = %s", grandchild) == [(own,)]
    _operation, answer = _register(ops, session, "subagent.request")
    assert ops.desk.rows("SELECT github_identity FROM hub.session WHERE id = %s", answer["child_session_id"]) == [
        (None,)
    ]
    state = ops.fleet.client(ops.device).get("/v1/device/state").json()
    found = {item["session_id"]: item["github_identity"] for item in state["sessions"]}
    assert (found[session], found[child], found[grandchild]) == (None, own, own)


def test_a_subagent_identity_waits_for_every_runner_of_the_org(ops: Ops) -> None:
    writer = ops.scene.writer
    _announce(ops, ops.scene.device)
    _task, session = ops.session()
    body = ops.request(session, "subagent.request")
    body["github_identity"] = _identity(ops, writer.id)
    response = ops.register(body)
    assert (response.status_code, response.json()) == (
        409,
        {"error": "peer_outdated", "detail": {"devices": [ops.scene.other_device.id]}},
    )
    assert ops.desk.rows("SELECT count(*) FROM hub.operation") == [(0,)]


def test_the_expiry_job_audits_the_identity(ops: Ops) -> None:
    writer = ops.scene.writer
    _task, session = ops.session()
    own = _identity(ops, writer.id)
    operation, _answer = _register(ops, session, github_identity=own, triggered_by=writer.id)
    ops.hub.clock.advance(3600)
    assert jobs.expire(ops.hub.app.state.hub) == 1
    [*_rest, (action, _result, _kind, _actor, detail)] = ops.audit(operation)
    assert (action, detail["github_identity"], detail["triggered_by"]) == ("operation.expired", own, writer.id)
