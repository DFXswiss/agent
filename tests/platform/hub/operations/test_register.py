"""U-HUB-operations: `POST /v1/device/operations` - checks H1-H6 one by one and in their order, the stored rows,
transitions, audit rows (R-11.1, R-11.3, R-11.7), frames and idempotency (03-api.md section 8.3)."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest

from agent_hub.crypto.encoding import format_time
from agent_hub.operations import register as register_module
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.operations.support import NOT_CONTRACT, Ops, tag
from tests.platform.hub.orgs.support import new_id


def _frames(published: MemoryPublisher, frame: str) -> list[dict[str, Any]]:
    return [dict(entry.data) for entry in published.items() if entry.frame == frame]


def _revoke_write(ops: Ops) -> None:
    ops.desk.execute(
        "UPDATE hub.wg_access SET access = 'read' WHERE wg_id = %s AND user_id = %s", ops.desk.wg, ops.scene.writer.id
    )


# --- accepted -------------------------------------------------------------------------------------------------------


def test_accepted_operation_rows_transitions_audit_and_frame(ops: Ops, published: MemoryPublisher) -> None:
    task, session = ops.session()
    published.clear()
    body = ops.request(session, "github.comment", dedupe="c1")
    response = ops.register(body)
    assert response.status_code == 200, response.text
    assert response.json() == {"state": "accepted"}
    operation = body["operation_id"]
    now = ops.hub.clock()
    rows = ops.desk.rows(
        "SELECT org_id::text, wg_id::text, task_id::text, session_id::text, device_id::text, actor_id::text, type,"
        " state, error_code, retryable, dedupe_epoch, dedupe_tag, duplicate_of, attempt, params_event_id::text,"
        " result_event_id, created_at, decided_at, started_at, finished_at FROM hub.operation WHERE id = %s",
        operation,
    )
    assert rows == [
        (
            ops.desk.org,
            ops.desk.wg,
            task,
            session,
            ops.device.id,
            ops.scene.writer.id,
            "github.comment",
            "accepted",
            None,
            False,
            1,
            tag("c1"),
            None,
            1,
            body["params_event"]["header"]["id"],
            None,
            now - timedelta(milliseconds=50),
            now,
            None,
            None,
        )
    ]
    assert ops.transitions(operation) == [(1, "received", None, ops.device.id), (2, "accepted", None, None)]
    assert ops.audit(operation) == [
        (
            "operation.accepted",
            "ok",
            "device",
            ops.device.id,
            {"type": "github.comment", "error_code": None, "duplicate_of": None, "local_check": "passed"},
        )
    ]
    contracts_registry().validate("audit/operation.accepted.json", ops.audit(operation)[0][4])
    assert ops.desk.rows(
        "SELECT kind, ref, task_id::text, session_id::text, author_id::text FROM hub.event WHERE id = %s",
        body["params_event"]["header"]["id"],
    ) == [("operation.params", operation, task, session, ops.device.id)]
    assert ops.slot_holder("c1") == operation
    frames = _frames(published, "operation")
    assert [frame["operation_id"] for frame in frames] == [operation]
    contracts_registry().validate("ws/sse/operation.json", frames[0])
    assert frames[0]["state"] == "accepted"


def test_types_without_dedupe_are_accepted_without_slot(ops: Ops) -> None:
    _task, session = ops.session()
    for op_type in (
        "github.list_repositories",
        "github.get_pull_request",
        "task.set_title",
        "github.comment",
        "github.list_issues",
        "github.get_issue",
        "github.update_issue",
        "github.list_pull_requests",
        "github.request_connection",
        "github.create_issue",
    ):
        operation, answer = ops.decide(session, op_type)
        assert answer == {"state": "accepted"}
        assert ops.operation(operation)[3:] == (None, None)
    assert ops.desk.rows("SELECT count(*) FROM hub.dedupe_slot") == [(0,)]


# --- H1 ---------------------------------------------------------------------------------------------------------------


def test_h1_unknown_session_and_session_of_another_device(ops: Ops) -> None:
    _task, session = ops.session()
    _other_task, foreign = ops.session(ops.scene.other_device)
    body = ops.request(session)
    body["session_id"] = new_id()
    assert ops.register(body).json() == {"error": "not_found"}
    response = ops.register(ops.request(foreign))
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert ops.desk.rows("SELECT count(*) FROM hub.operation") == [(0,)]
    assert ops.desk.rows("SELECT count(*) FROM hub.event WHERE kind = 'operation.params'") == [(0,)]


@pytest.mark.parametrize("ended", ["stopped", "failed"])
def test_h1_ended_session_is_denied_before_the_local_check(ops: Ops, ended: str) -> None:
    _task, session = ops.session()
    ops.desk.execute("UPDATE hub.session SET state = %s WHERE id = %s", ended, session)
    operation, answer = ops.decide(session, local_check="rejected", error_code="params_invalid")
    assert answer == {"state": "denied", "error_code": "rights_denied"}
    assert ops.audit(operation)[0][:2] == ("operation.denied", "denied")
    assert ops.audit(operation)[0][4]["local_check"] == "rejected"


def test_h1_closed_task_is_denied(ops: Ops) -> None:
    task, session = ops.session()
    ops.desk.execute("UPDATE hub.task SET state = 'closed', closed_at = now() WHERE id = %s", task)
    assert ops.decide(session)[1] == {"state": "denied", "error_code": "rights_denied"}


# --- local check ------------------------------------------------------------------------------------------------------


def test_local_rejection_is_stored_without_h2_to_h6(ops: Ops) -> None:
    _task, session = ops.session()
    holder, _answer = ops.decide(session, dedupe="same")
    _revoke_write(ops)
    operation, answer = ops.decide(
        session, "github.unknown", dedupe="same", local_check="rejected", error_code="params_invalid"
    )
    assert answer == {"state": "rejected", "error_code": "params_invalid"}
    assert ops.operation(operation) == ("rejected", "params_invalid", None, 1, tag("same"))
    assert ops.slot_holder("same") == holder
    assert ops.transitions(operation) == [(1, "received", None, ops.device.id), (2, "rejected", "params_invalid", None)]
    audit = ops.audit(operation)
    assert [(row[0], row[1]) for row in audit] == [("operation.rejected", "rejected")]
    assert audit[0][4] == {
        "type": "github.unknown",
        "error_code": "params_invalid",
        "duplicate_of": None,
        "local_check": "rejected",
    }
    contracts_registry().validate("audit/operation.rejected.json", audit[0][4])


# --- H2 ---------------------------------------------------------------------------------------------------------------


def test_h2_owner_without_write_access_is_denied_before_h3(ops: Ops) -> None:
    _task, session = ops.session()
    _revoke_write(ops)
    operation, answer = ops.decide(session, "github.unknown")
    assert answer == {"state": "denied", "error_code": "rights_denied"}
    assert ops.audit(operation)[0][:2] == ("operation.denied", "denied")
    contracts_registry().validate("audit/operation.denied.json", ops.audit(operation)[0][4])


def test_h2_archived_workgroup_is_denied(ops: Ops) -> None:
    _task, session = ops.session()
    ops.desk.execute("UPDATE hub.wg SET archived_seq = 99 WHERE id = %s", ops.desk.wg)
    assert ops.decide(session)[1] == {"state": "denied", "error_code": "rights_denied"}


# --- H3 ---------------------------------------------------------------------------------------------------------------


def test_h3_unknown_type_is_rejected_before_h5(ops: Ops) -> None:
    _task, session = ops.session()
    holder, _answer = ops.decide(session, dedupe="t")
    operation, answer = ops.decide(session, "github.delete_repository", dedupe="t")
    assert answer == {"state": "rejected", "error_code": "unknown_type"}
    assert ops.slot_holder("t") == holder
    assert ops.audit(operation)[0][4]["local_check"] == "passed"


# --- H5 / H6 ----------------------------------------------------------------------------------------------------------


def test_h5_duplicate_inside_the_window_returns_the_original(ops: Ops, published: MemoryPublisher) -> None:
    _task, session = ops.session()
    original, _answer = ops.decide(session, dedupe="comment")
    ops.hub.clock.advance(86399)
    published.clear()
    duplicate, answer = ops.decide(session, dedupe="comment")
    assert answer == {"state": "duplicate", "duplicate_of": original, "original": {"state": "accepted"}}
    assert ops.operation(duplicate) == ("duplicate", None, original, 1, tag("comment"))
    assert ops.slot_holder("comment") == original
    audit = ops.audit(duplicate)
    assert [(row[0], row[1]) for row in audit] == [("operation.duplicate", "duplicate")]
    assert audit[0][4]["duplicate_of"] == original
    contracts_registry().validate("audit/operation.duplicate.json", audit[0][4])
    assert [frame["state"] for frame in _frames(published, "operation")] == ["duplicate"]
    result = ops.finish(original)
    assert ops.decide(session, dedupe="comment")[1] == {
        "state": "duplicate",
        "duplicate_of": original,
        "original": {"state": "succeeded", "result_event_id": result},
    }


def test_h6_after_the_window_takes_the_slot_over(ops: Ops) -> None:
    _task, session = ops.session()
    original, _answer = ops.decide(session, "github.checkout", dedupe="checkout")
    ops.hub.clock.advance(599)
    assert ops.decide(session, "github.checkout", dedupe="checkout")[1]["state"] == "duplicate"
    ops.hub.clock.advance(1)
    newer, answer = ops.decide(session, "github.checkout", dedupe="checkout")
    assert answer == {"state": "accepted"}
    assert ops.slot_holder("checkout") == newer
    assert ops.desk.rows(
        "SELECT expires_at - %s FROM hub.dedupe_slot WHERE operation_id = %s", ops.hub.clock(), newer
    ) == [(timedelta(seconds=600),)]
    assert ops.state(original) == "accepted"


@pytest.mark.parametrize(
    ("op_type", "seconds"),
    [
        ("github.create_branch", 86400),
        ("github.commit", 86400),
        ("github.push", 86400),
        ("github.create_pull_request", 604800),
        ("github.comment", 86400),
        ("github.create_issue", 86400),
        ("subagent.request", 600),
        ("github.list_repositories", 86400),
        ("github.list_issues", 86400),
        ("github.update_issue", 86400),
    ],
)
def test_window_per_type(ops: Ops, op_type: str, seconds: int) -> None:
    _task, session = ops.session()
    operation, answer = ops.decide(session, op_type, dedupe="w")
    assert answer["state"] == "accepted"
    assert ops.desk.rows(
        "SELECT expires_at - %s FROM hub.dedupe_slot WHERE operation_id = %s", ops.hub.clock(), operation
    ) == [(timedelta(seconds=seconds),)]


@pytest.mark.parametrize("ending", ["failed", "expired"])
def test_slot_released_after_failure_or_expiry(ops: Ops, ending: str) -> None:
    _task, session = ops.session()
    original, _answer = ops.decide(session, dedupe="again")
    ops.finish(original, ending)
    assert ops.slot_holder("again") is None
    retry, answer = ops.decide(session, dedupe="again")
    assert answer == {"state": "accepted"}
    assert ops.slot_holder("again") == retry


def test_slot_of_an_operation_that_no_longer_holds_it_is_taken_over(ops: Ops) -> None:
    _task, session = ops.session()
    original, _answer = ops.decide(session, dedupe="stale")
    ops.desk.execute("UPDATE hub.operation SET state = 'failed' WHERE id = %s", original)
    newer, answer = ops.decide(session, dedupe="stale")
    assert answer == {"state": "accepted"}
    assert ops.slot_holder("stale") == newer


def test_slots_are_per_workgroup_and_epoch(ops: Ops) -> None:
    _task, session = ops.session()
    original, _answer = ops.decide(session, dedupe="scoped")
    ops.desk.execute("UPDATE hub.dedupe_slot SET wg_id = %s WHERE operation_id = %s", new_id(), original)
    assert ops.decide(session, dedupe="scoped")[1] == {"state": "accepted"}


# --- consistency of the request ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "pointers"),
    [
        ({"ref": "other"}, ["/params_event/header/ref"]),
        ({"task": "other"}, ["/params_event/header/task"]),
        ({"session": ""}, ["/params_event/header/session"]),
    ],
)
def test_params_event_header_must_name_the_operation(ops: Ops, change: dict[str, str], pointers: list[str]) -> None:
    _task, session = ops.session()
    header: dict[str, Any] = {key: (new_id() if value == "other" else value) for key, value in change.items()}
    response = ops.register(ops.request(session, **header))
    assert (response.status_code, response.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": pointers}},
    )
    assert ops.desk.rows("SELECT count(*) FROM hub.event WHERE kind = 'operation.params'") == [(0,)]
    assert ops.desk.rows("SELECT count(*) FROM hub.operation") == [(0,)]


def test_dedupe_epoch_must_be_the_params_epoch(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session, dedupe="e")
    body["dedupe"]["epoch"] = 2
    response = ops.register(body)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/dedupe/epoch"]}}


def test_workgroup_must_be_the_sessions(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session)
    body["wg_id"] = new_id()
    response = ops.register(body)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/params_event/header/scope_id"]}}


def test_task_must_be_the_sessions(ops: Ops) -> None:
    _task, session = ops.session()
    other_task, _other = ops.session()
    body = ops.request(session, task=other_task)
    body["task_id"] = other_task
    response = ops.register(body)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/task_id"]}}


@pytest.mark.parametrize(("local_check", "error_code"), [("passed", "params_invalid"), ("rejected", None)])
def test_error_code_exactly_with_a_local_rejection(ops: Ops, local_check: str, error_code: str | None) -> None:
    _task, session = ops.session()
    body = ops.request(session, local_check=local_check, error_code=error_code)
    response = ops.register(body, extensions=NOT_CONTRACT)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/error_code"]}}


def test_params_event_of_another_author_is_refused(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session, device=ops.scene.other_device)
    response = ops.register(body)
    assert (response.status_code, response.json()) == (403, {"error": "forbidden"})
    assert ops.desk.rows("SELECT count(*) FROM hub.operation") == [(0,)]


# --- idempotency ------------------------------------------------------------------------------------------------------


def test_repetition_returns_the_original_decision(ops: Ops) -> None:
    _task, session = ops.session()
    holder, _answer = ops.decide(session, dedupe="r")
    body = ops.request(session, dedupe="r")
    first = ops.register(body).json()
    result = ops.finish(holder, "failed")
    actions = "SELECT count(*) FROM hub.audit WHERE action NOT LIKE 'request.%%'"
    audit_count = ops.desk.rows(actions)
    again = ops.register(body)
    assert again.status_code == 200
    assert first == {"state": "duplicate", "duplicate_of": holder, "original": {"state": "accepted"}}
    assert again.json() == {**first, "original": {"state": "failed", "result_event_id": result}, "replayed": True}
    assert ops.desk.rows(actions) == audit_count  # the repeat leaves only the pipeline's generic row (ADR-063)
    assert ops.desk.rows("SELECT count(*) FROM hub.audit WHERE action = 'request.write'") == [(1,)]
    assert len(ops.transitions(body["operation_id"])) == 2
    rejected = ops.request(session, local_check="rejected", error_code="size_exceeded")
    assert ops.register(rejected).json() == {"state": "rejected", "error_code": "size_exceeded"}
    assert ops.register(rejected).json() == {"state": "rejected", "error_code": "size_exceeded", "replayed": True}
    accepted = ops.request(session, "task.set_title")
    ops.register(accepted)
    assert ops.register(accepted).json() == {"state": "accepted", "replayed": True}


def test_repetition_with_other_content_is_a_conflict(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session, dedupe="x")
    ops.register(body)
    for change in ({"type": "github.push"}, {"dedupe": None}, {"requested_at": "2026-01-01T00:00:00.000Z"}):
        response = ops.register({**body, **change})
        assert (response.status_code, response.json()) == (409, {"error": "id_conflict"}), change
    other_event = ops.request(session, operation=body["operation_id"], dedupe="x")
    response = ops.register(other_event)
    assert (response.status_code, response.json()) == (409, {"error": "id_conflict"})
    assert ops.desk.rows("SELECT count(*) FROM hub.event WHERE kind = 'operation.params'") == [(1,)]


def test_repetition_with_another_local_check_is_a_conflict(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session, "github.unknown")
    assert ops.register(body).json() == {"state": "rejected", "error_code": "unknown_type"}
    local = {**body, "local_check": "rejected", "error_code": "unknown_type"}
    assert ops.register(local).json() == {"error": "id_conflict"}
    other = ops.request(session, local_check="rejected", error_code="size_exceeded")
    ops.register(other)
    assert ops.register({**other, "error_code": "params_invalid"}).json() == {"error": "id_conflict"}


def test_deadlock_victim_is_registered_once_more(ops: Ops, monkeypatch: pytest.MonkeyPatch) -> None:
    _task, session = ops.session()
    calls: list[int] = []
    real = register_module.register

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.errors.DeadlockDetected()
        return real(*args, **kwargs)

    monkeypatch.setattr("agent_hub.operations.routes.register", flaky)
    assert ops.decide(session)[1] == {"state": "accepted"}
    assert len(calls) == 2


# --- ADR-014 ----------------------------------------------------------------------------------------------------------


def test_no_pull_request_table(ops: Ops) -> None:
    tables = ops.desk.rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'hub'"
        " AND (table_name LIKE '%%pull%%' OR table_name LIKE 'pr\\_%%' OR table_name LIKE '%%\\_pr')"
    )
    # `pr_link` holds only an opaque HMAC tag of a pull request and its task (KOR-33, ADR-072 section 8)
    assert tables == [("pr_link",)]
    columns = ops.desk.rows(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'hub' AND table_name = 'operation'"
    )
    assert not any("pull" in name or "url" in name or "title" in name for (name,) in columns)


def test_params_event_must_be_of_kind_operation_params(ops: Ops) -> None:
    task, session = ops.session()
    body = ops.request(session)
    body["params_event"] = ops.scene.wg_event(
        ops.device, "operation.result", task=task, session=session, ref=body["operation_id"]
    )
    response = ops.register(body, extensions=NOT_CONTRACT)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/params_event/header/kind"]}}
    assert ops.desk.rows("SELECT count(*) FROM hub.event WHERE kind LIKE 'operation.%%'") == [(0,)]


def test_requested_at_at_most_120_seconds_ahead(ops: Ops) -> None:
    _task, session = ops.session()
    body = ops.request(session)
    body["requested_at"] = format_time(ops.hub.clock() + timedelta(seconds=121))
    response = ops.register(body)
    assert response.json() == {"error": "request_invalid", "detail": {"pointers": ["/requested_at"]}}
    body["requested_at"] = format_time(ops.hub.clock() + timedelta(seconds=120))
    assert ops.register(body).json() == {"state": "accepted"}


# --- concurrency ------------------------------------------------------------------------------------------------------


def _parallel(ops: Ops, first: dict[str, Any], second: dict[str, Any]) -> list[dict[str, Any]]:
    """Register `first` in an open transaction, start `second` in another thread (it must wait for the first one's
    locks), then commit the first; both answers."""
    device, now, root = UUID(ops.device.id), ops.hub.clock(), ops.hub.config.platform_root_key
    answers: dict[str, dict[str, Any]] = {}

    def run(name: str, body: dict[str, Any]) -> None:
        request = register_module.RegistrationRequest.model_validate(body)
        with ops.hub.pool.connection() as connection, connection.transaction():
            answers[name] = register_module.register(connection, device, request, now=now, root_key=root).body

    with ops.hub.pool.connection() as connection:
        with connection.transaction():
            request = register_module.RegistrationRequest.model_validate(first)
            answers["first"] = register_module.register(connection, device, request, now=now, root_key=root).body
            thread = threading.Thread(target=run, args=("second", second))
            thread.start()
            thread.join(0.5)
            assert thread.is_alive()
        thread.join(30)
    return [answers["first"], answers["second"]]


def test_parallel_registrations_of_one_tag_accept_exactly_one(ops: Ops) -> None:
    _task, session = ops.session()
    first, second = ops.request(session, dedupe="race"), ops.request(session, dedupe="race")
    assert _parallel(ops, first, second) == [
        {"state": "accepted"},
        {"state": "duplicate", "duplicate_of": first["operation_id"], "original": {"state": "accepted"}},
    ]


def test_parallel_subagent_requests_take_the_last_place_once(ops: Ops) -> None:
    task, session = ops.session()
    ops.desk.execute(
        "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
        " created_at) SELECT %s::uuid, task_id, org_id, wg_id, owner_id, device_id, id, 1, 'working', now()"
        " FROM hub.session WHERE id = %s UNION ALL SELECT %s::uuid, task_id, org_id, wg_id, owner_id, device_id, id, 1,"
        " 'working', now() FROM hub.session WHERE id = %s",
        new_id(),
        session,
        new_id(),
        session,
    )
    answers = _parallel(ops, ops.request(session, "subagent.request"), ops.request(session, "subagent.request"))
    assert answers[0]["state"] == "accepted"
    assert answers[1] == {"state": "denied", "error_code": "limit_exceeded"}
    assert ops.desk.rows("SELECT count(*) FROM hub.session WHERE task_id = %s AND depth = 1", task) == [(3,)]
