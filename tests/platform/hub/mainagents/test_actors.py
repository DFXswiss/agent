"""U-HUB-mainagents (actors, ADR-095 section 4; KOR-52): what a session does is written with its agent as actor -
`main_agent` for a main session, `subagent` for a child, chosen from the session's depth - on behalf of the person
whose instruction it followed, for exactly the rows of the closed rule (operation steps, the start of a child, outbox
`audit` items that name a session); the key `on_behalf_of` is hashed only when set, so earlier rows keep their hashes;
the audit API filters by it, accepts a session as `actor` and names the agents of a page in `actors`."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from agent_core.signing import sha256_hex
from agent_hub.audit.chain import AuditRow, AuditRowInvalid, append_audit, row_json, verify_chain, verify_rows
from agent_hub.crypto.encoding import canonical, format_time
from tests.platform.hub.conftest import uuid7
from tests.platform.hub.mainagents.support import NOT_CONTRACT
from tests.platform.hub.operations.support import Ops

SUBAGENT = "subagent.request"


def _register(ops: Ops, session: str, op_type: str = "github.comment", triggered_by: str | None = None) -> str:
    body = ops.request(session, op_type)
    if triggered_by is not None:
        body["triggered_by"] = triggered_by
    response = ops.register(body)
    assert response.status_code == 200, response.text
    if op_type == SUBAGENT:
        child = str(response.json()["child_session_id"])
        ops.scene.sessions[child] = ops.scene.sessions[session]
        return child
    return str(body["operation_id"])


def _actors(ops: Ops, where: str, *params: object) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in ops.desk.rows(
            "SELECT action, actor_kind, actor_id::text, on_behalf_of::text, device_id::text, session_id::text"
            f" FROM hub.audit WHERE {where} ORDER BY seq",
            *params,
        )
    ]


def test_the_steps_of_an_operation_are_its_sessions_agent(ops: Ops) -> None:
    writer, reader, device = ops.scene.writer, ops.scene.reader, ops.device.id
    _task, main = ops.session()
    operation = _register(ops, main, triggered_by=writer.id)
    ops.finish(operation)
    assert _actors(ops, "target_id = %s", operation) == [
        (f"operation.{state}", "main_agent", main, writer.id, device, main)
        for state in ("accepted", "executing", "succeeded")
    ]
    # without a single person (a turn with several authors or none) the agent acts on behalf of nobody
    nobody = _register(ops, main)
    assert _actors(ops, "target_id = %s", nobody) == [("operation.accepted", "main_agent", main, None, device, main)]
    # a refused registration is the agent's as well
    denied = ops.request(main, "github.comment", local_check="rejected", error_code="params_invalid")
    assert ops.register(denied).json() == {"state": "rejected", "error_code": "params_invalid"}
    assert _actors(ops, "target_id = %s", denied["operation_id"])[0][1:3] == ("main_agent", main)
    # H2a refuses a `triggered_by` outside the org: the agent acted on behalf of nobody (the detail keeps the report)
    stranger = ops.scene.stranger.id
    forged = _register(ops, main, triggered_by=stranger)
    assert _actors(ops, "target_id = %s", forged) == [("operation.denied", "main_agent", main, None, device, main)]
    assert ops.audit(forged)[0][4]["triggered_by"] == stranger
    # the start of a child: the requesting agent on behalf of the child's person; the task's refresh stays the device's
    child = _register(ops, main, SUBAGENT, triggered_by=reader.id)
    assert _actors(ops, "action = 'session.requested' AND session_id = %s", child) == [
        ("session.requested", "main_agent", main, reader.id, device, child)
    ]
    assert ops.desk.rows("SELECT triggered_by::text, agent_path FROM hub.session WHERE id = %s", child) == [
        (reader.id, "1")
    ]
    assert {row[1] for row in _actors(ops, "action LIKE 'task.%%'")} <= {"device"}
    # the child's own operation: a subagent on behalf of the operation's person, not the session's
    own = _register(ops, child, triggered_by=writer.id)
    assert _actors(ops, "target_id = %s", own) == [("operation.accepted", "subagent", child, writer.id, device, child)]
    grandchild = _register(ops, child, SUBAGENT)
    assert _actors(ops, "action = 'session.requested' AND session_id = %s", grandchild) == [
        ("session.requested", "subagent", child, None, device, grandchild)
    ]


def test_outbox_audit_items_of_a_session_are_its_agents(ops: Ops) -> None:
    writer, reader, device = ops.scene.writer, ops.scene.reader, ops.device.id
    _task, main = ops.session()
    ops.desk.execute("UPDATE hub.session SET triggered_by = %s WHERE id = %s", writer.id, main)
    child = _register(ops, main, SUBAGENT, triggered_by=reader.id)
    seq = ops.outbox.position()
    pushed = [
        ops.outbox.audit(seq + 1, "hook.denied", {"tool": "Bash", "rule": 3}, session_id=main, result="denied"),
        ops.outbox.audit(seq + 2, "hook.denied", {"tool": "Bash", "rule": 4}, session_id=child, result="denied"),
        ops.outbox.audit(seq + 3, "runner.alert", {"alert": "vm_budget_exceeded", "value": 2}, target_kind="device"),
    ]
    assert {status for _seq, status, _error in ops.outbox.statuses(*pushed)} == {"ok"}
    assert _actors(ops, "action IN ('hook.denied', 'runner.alert')") == [
        ("hook.denied", "main_agent", main, writer.id, device, main),
        ("hook.denied", "subagent", child, reader.id, device, child),
        ("runner.alert", "device", device, None, device, None),
    ]
    # a person who is no longer an active member of the org (here: never was) is nobody the agent acts for
    ops.desk.execute("UPDATE hub.session SET triggered_by = %s WHERE id = %s", ops.scene.stranger.id, main)
    later = ops.outbox.audit(seq + 4, "hook.denied", {"tool": "Bash", "rule": 5}, session_id=main, result="denied")
    assert {status for _seq, status, _error in ops.outbox.statuses(later)} == {"ok"}
    assert _actors(ops, "action = 'hook.denied'")[-1] == ("hook.denied", "main_agent", main, None, device, main)


def _page(ops: Ops, **params: str) -> dict[str, Any]:
    response = ops.scene.reader.browser.get(f"/v1/orgs/{ops.desk.org}/audit", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_the_audit_api_names_the_agents_and_filters_by_person(ops: Ops) -> None:
    writer, reader = ops.scene.writer, ops.scene.reader
    task, main = ops.session()
    number = ops.desk.rows("SELECT number FROM hub.task WHERE id = %s", task)[0][0]
    child = _register(ops, main, SUBAGENT, triggered_by=reader.id)
    by_child = _register(ops, child, triggered_by=writer.id)
    _register(ops, main)
    page = _page(ops, actor=main)
    assert {row["actor_id"] for row in page["rows"]} == {main}
    assert {row["actor_kind"] for row in page["rows"]} == {"main_agent"}
    assert page["actors"] == {
        main: {"task_id": task, "number": number, "agent_path": None, "parent_session_id": None},
    }
    assert "on_behalf_of" not in page["rows"][0]  # the newest: on behalf of nobody
    assert page["rows"][1]["on_behalf_of"] == reader.id  # the start of the child
    for_writer = _page(ops, on_behalf_of=writer.id)
    assert [(row["actor_id"], row["target_id"]) for row in for_writer["rows"]] == [(child, by_child)]
    assert for_writer["actors"] == {
        child: {"task_id": task, "number": number, "agent_path": "1", "parent_session_id": main},
    }
    both = _page(ops, task=task)
    assert set(both["actors"]) == {main, child}
    assert _page(ops, action="read")["actors"] == {}
    refused = ops.scene.reader.browser.get(
        f"/v1/orgs/{ops.desk.org}/audit", params={"on_behalf_of": "someone"}, extensions=NOT_CONTRACT
    )
    assert (refused.status_code, refused.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/on_behalf_of"]}},
    )


def test_a_row_without_a_person_keeps_its_former_hash(ops: Ops) -> None:
    at = ops.hub.clock()
    session, person = uuid7(), uuid7()
    plain = AuditRow(
        at=at,
        actor_kind="subagent",
        actor_id=session,
        action="session.requested",
        result="ok",
        session_id=session,
        target_kind="session",
        target_id=str(session),
        detail={"reason": None, "model_access": None, "model_id": None},
    )
    with ops.hub.pool.connection() as connection:
        stored = append_audit(connection, plain)
        attributed = append_audit(connection, replace(plain, on_behalf_of=person))
        assert verify_chain(connection, stored.seq, attributed.seq).ok
        hashes = dict(
            connection.execute(
                "SELECT seq, hash FROM hub.audit WHERE seq IN (%s, %s)", (stored.seq, attributed.seq)
            ).fetchall()
        )
    # the hashed form of a row without a person is exactly the form before KOR-52 (no key `on_behalf_of`)
    former = {
        "seq": stored.seq,
        "at": format_time(stored.row.at),
        "actor_id": str(session),
        "actor_kind": "subagent",
        "action": "session.requested",
        "result": "ok",
        "org_id": None,
        "wg_id": None,
        "task_id": None,
        "session_id": str(session),
        "device_id": None,
        "target_kind": "session",
        "target_id": str(session),
        "detail": {"reason": None, "model_access": None, "model_id": None},
        "event_id": None,
    }
    assert row_json(stored.seq, stored.row) == former
    text = b"\n".join(
        [b"agent-audit-v1", str(stored.seq).encode(), canonical(former), (stored.prev_hash or "-").encode()]
    )
    assert hashes[stored.seq] == stored.hash == sha256_hex(text)
    assert row_json(attributed.seq, attributed.row)["on_behalf_of"] == str(person)
    assert hashes[attributed.seq] == attributed.hash
    # the person is covered by the hash: another one does not verify
    forged = replace(attributed, row=replace(attributed.row, on_behalf_of=uuid7()))
    assert not verify_rows([stored, forged], stored.seq, stored.prev_hash).ok
    assert verify_rows([stored, attributed], stored.seq, stored.prev_hash).ok


def test_only_an_agent_acts_on_behalf_of_a_person(ops: Ops) -> None:
    row = AuditRow(
        at=ops.hub.clock(),
        actor_kind="user",
        actor_id=UUID(ops.scene.reader.id),
        action="read.me.audit",
        result="ok",
        on_behalf_of=UUID(ops.scene.writer.id),
    )
    with ops.hub.pool.connection() as connection, pytest.raises(AuditRowInvalid, match="on_behalf_of"):
        append_audit(connection, row)
