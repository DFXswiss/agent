"""U-GST-control: the control process over its real unix socket (03-api.md section 7.1, 05-runner.md section 5.1)."""

from __future__ import annotations

import grp
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import __main__ as cli
from agent_guest import control as control_module
from agent_guest import registry
from agent_guest import server as server_module
from agent_guest.apisock import ApiClient, ApiSockError, token_sha256
from agent_guest.config import GuestConfig
from agent_guest.control import (
    ControlContext,
    ControlService,
    StartupError,
    load_info,
    main,
)
from agent_guest.journal import JournalError
from agent_guest.schema import SchemaSet
from agent_guest.state import Session, StateError
from tests.platform.guest.conftest import (
    BOOT_ID,
    CONTROL_TOKEN,
    SESSION_ID,
    SESSION_TOKEN,
    ControlClient,
    FakeClock,
    make_session,
)

VM_ID = "vm-0a1b2c3d"
ORG_ID = "01a1ee56-5e00-7001-8f1b-340691d99f01"
WG_ID = "01a1ee56-5e00-7002-8f1b-340691d99f02"
OTHER_ID = "01a1ee56-5e00-7009-8f1b-340691d99f09"
HOOK = {"event": "Stop", "decision": "none"}  # a routine PreToolUse allow would be counted (hook_summary.py)
OPERATION = {
    "operation_id": "01a1ee56-5e00-7004-8f1b-340691d99f04",
    "type": "github.comment",
    "params": {"body": "hi"},
    "requested_at": "2026-11-21T10:00:00.000Z",
}
QUERY = {"tool": "get_context", "arguments": {}}
WORKVM = {"tool": "workvm_exec", "arguments": {"command": "npm test", "wait_seconds": 5}}
ANSWER_ERROR = {"status": "error", "error": {"error": "unavailable", "message": "The hub is unreachable."}}


def raw_request(path: str, data: bytes) -> bytes:
    """Send raw bytes to a unix socket and return everything until the server closes or 2 s pass."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(path)
        client.sendall(data)
        chunks = []
        try:
            while chunk := client.recv(65536):
                chunks.append(chunk)
                if b"\r\n\r\n" in b"".join(chunks) and b'"error"' in b"".join(chunks):
                    break
        except TimeoutError:
            pass
        return b"".join(chunks)


def auth() -> bytes:
    return f"Authorization: Bearer {CONTROL_TOKEN}\r\n".encode()


# ---------------------------------------------------------------- authentication


def test_health_unassigned(control_client: ControlClient, schemas: SchemaSet) -> None:
    reply = control_client.request("GET", "/v1/health")
    assert reply.status == 200
    body = reply.json()
    assert body == {
        "guest_version": "0.6.0",
        "claude_version": "2.1.330",
        "state": "unassigned",
        "sessions": [],
        "landlock_abi": 6,
        "boot_id": BOOT_ID,
    }
    assert schemas.errors("guest/health.response.json", body) == []
    assert reply.headers["Content-Type"] == "application/json"
    assert reply.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "header",
    [None, "Bearer wrong", f"Basic {CONTROL_TOKEN}", f"Bearer  {CONTROL_TOKEN}", f"Bearer {CONTROL_TOKEN}x", "Bearer"],
)
def test_wrong_or_missing_token(control_client: ControlClient, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    for target in ("/v1/health", "/v1/unknown"):
        reply = control_client.request("GET", target, token=None, headers=headers)
        assert reply.status == 401
        assert reply.json() == {"error": "unauthorized"}


def test_two_authorization_headers(guest_config: GuestConfig, control: ControlService) -> None:
    data = (
        b"GET /v1/health HTTP/1.1\r\nHost: x\r\n" + auth() + auth() + b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    assert b" 401 " in raw_request(guest_config.control_socket, data).split(b"\r\n")[0]


def test_token_file_is_read_per_request(control_client: ControlClient, guest_config: GuestConfig) -> None:
    token_file = Path(guest_config.control_token_file)
    token_file.write_text("rotated", encoding="ascii")
    assert control_client.request("GET", "/v1/health").status == 401
    assert control_client.request("GET", "/v1/health", token="rotated").status == 200
    token_file.write_text("\n", encoding="ascii")
    assert control_client.request("GET", "/v1/health", token="").status == 401
    assert control_client.request("GET", "/v1/health", headers={"Authorization": "Bearer "}, token=None).status == 401
    token_file.unlink()
    assert control_client.request("GET", "/v1/health", token="rotated").status == 401


def test_unknown_path_and_method(control_client: ControlClient, schemas: SchemaSet) -> None:
    for method, target in (("GET", "/v1/nothing"), ("DELETE", "/v1/health"), ("GET", "/v1/assign")):
        reply = control_client.request(method, target)
        assert reply.status == 404
        assert reply.json() == {"error": "not_found"}
        assert schemas.errors("guest/error.response.json", reply.json()) == []


def test_absolute_form_target_is_not_found(guest_config: GuestConfig, control: ControlService) -> None:
    data = b"GET http://x/v1/health HTTP/1.1\r\nHost: x\r\n" + auth() + b"Connection: close\r\n\r\n"
    assert b" 404 " in raw_request(guest_config.control_socket, data).split(b"\r\n")[0]


def test_health_refuses_query_parameters(control_client: ControlClient) -> None:
    reply = control_client.request("GET", "/v1/health?x=1")
    assert reply.status == 400
    assert reply.json() == {"error": "request_invalid", "detail": {"pointers": ["/x"]}}


# ---------------------------------------------------------------- assignment and health states


def test_assign_once(control_client: ControlClient, context: ControlContext, schemas: SchemaSet) -> None:
    body = {"vm_id": VM_ID, "org_id": ORG_ID, "wg_id": WG_ID}
    assert schemas.errors("guest/assign.request.json", body) == []
    reply = control_client.request("POST", "/v1/assign", body)
    assert (reply.status, reply.json()) == (200, {})
    assert control_client.request("POST", "/v1/assign", body).status == 200
    for other in ({**body, "wg_id": OTHER_ID}, {**body, "vm_id": "vm-ffffffff"}):
        reply = control_client.request("POST", "/v1/assign", other)
        assert (reply.status, reply.json()) == (409, {"error": "already_assigned"})
    health = control_client.request("GET", "/v1/health").json()
    assert (health["state"], health["org_id"], health["wg_id"]) == ("assigned", ORG_ID, WG_ID)
    assert schemas.errors("guest/health.response.json", health) == []
    context.state.mark_broken()
    health = control_client.request("GET", "/v1/health").json()
    assert health["state"] == "broken"
    assert schemas.errors("guest/health.response.json", health) == []


def test_assignment_survives_a_restart(guest_config: GuestConfig, clock: FakeClock) -> None:
    first = ControlService(guest_config, clock)
    first.start()
    try:
        client = ControlClient(guest_config.control_socket)
        assert client.request("POST", "/v1/assign", {"vm_id": VM_ID, "org_id": ORG_ID, "wg_id": WG_ID}).status == 200
        assert first.context is not None
        first.context.state.save_session(make_session())
    finally:
        first.stop()
    second = ControlService(guest_config, clock, startup=())  # no session manager on this test machine
    second.start()
    try:
        health = ControlClient(guest_config.control_socket).request("GET", "/v1/health").json()
        assert health["state"] == "assigned"
        assert health["sessions"] == [{"session_id": SESSION_ID, "state": "working"}]
    finally:
        second.stop()


def test_broken_vm_is_never_assigned(
    control_client: ControlClient, context: ControlContext, schemas: SchemaSet
) -> None:
    context.state.mark_broken()
    health = control_client.request("GET", "/v1/health").json()
    assert health["state"] == "broken"
    assert schemas.errors("guest/health.response.json", health) == []
    reply = control_client.request("POST", "/v1/assign", {"vm_id": VM_ID, "org_id": ORG_ID, "wg_id": WG_ID})
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})


def test_health_lists_every_session_sorted(
    control_client: ControlClient, context: ControlContext, schemas: SchemaSet
) -> None:
    for index in reversed(range(200)):
        session_id = f"01a1ee56-5e00-7003-8f1b-{index:012x}"
        context.state.save_session(make_session(session_id, f"token-{index}", "idle"))
    health = control_client.request("GET", "/v1/health").json()
    sessions = health["sessions"]
    assert len(sessions) == 200  # no cap (ADR-088 section 3)
    assert sessions[0] == {"session_id": "01a1ee56-5e00-7003-8f1b-000000000000", "state": "idle"}
    assert sessions == sorted(sessions, key=lambda row: row["session_id"])
    assert schemas.errors("guest/health.response.json", health) == []


@pytest.mark.parametrize(
    ("body", "pointers"),
    [
        ({"vm_id": VM_ID, "org_id": ORG_ID}, ["/wg_id"]),
        ({"vm_id": "vm-1", "org_id": ORG_ID, "wg_id": WG_ID}, ["/vm_id"]),
        ({"vm_id": VM_ID, "org_id": ORG_ID, "wg_id": WG_ID, "x": 1}, ["/x"]),
        ([], [""]),
    ],
)
def test_assign_request_invalid(control_client: ControlClient, body: object, pointers: list[str]) -> None:
    reply = control_client.request("POST", "/v1/assign", body)
    assert reply.status == 400
    assert reply.json() == {"error": "request_invalid", "detail": {"pointers": pointers}}


@pytest.mark.parametrize(
    "raw",
    [b"{", b'{"vm_id":"a","vm_id":"b"}', b'{"x": NaN}', b"\xff\xfe", b""],
)
def test_malformed_json(control_client: ControlClient, raw: bytes) -> None:
    reply = control_client.request("POST", "/v1/assign", raw=raw)
    assert (reply.status, reply.json()) == (400, {"error": "request_invalid"})


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (b"POST /v1/assign HTTP/1.1\r\nTransfer-Encoding: chunked\r\n", b"400"),
        (b"POST /v1/assign HTTP/1.1\r\n", b"400"),
        (b"POST /v1/assign HTTP/1.1\r\nContent-Length: -1\r\n", b"400"),
        (b"POST /v1/assign HTTP/1.1\r\nContent-Length: 99999999\r\n", b"413"),
        (b"GET /v1/health HTTP/1.1\r\nContent-Length: 3\r\n", b"400"),
        (b"GET /v1/health HTTP/1.1\r\nContent-Length: x\r\n", b"400"),
        (b"POST /v1/assign HTTP/1.1\r\nContent-Length: \xc2\xb2\r\n", b"400"),
        (b"POST /v1/assign HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\n", b"400"),
    ],
)
def test_body_framing(guest_config: GuestConfig, control: ControlService, head: bytes, status: bytes) -> None:
    answer = raw_request(guest_config.control_socket, head + b"Host: x\r\n" + auth() + b"\r\n")
    first, _, rest = answer.partition(b"\r\n")
    assert first.split(b" ")[1] == status
    assert b"Connection: close" in rest


def test_keep_alive_serves_sequential_requests(guest_config: GuestConfig, control: ControlService) -> None:
    one = b"GET /v1/health HTTP/1.1\r\nHost: x\r\n" + auth() + b"\r\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(guest_config.control_socket)
        reader = client.makefile("rb")
        for _ in range(3):
            client.sendall(one)
            assert reader.readline().startswith(b"HTTP/1.1 200")
            length = 0
            while (line := reader.readline()) != b"\r\n":
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            assert json.loads(reader.read(length))["boot_id"] == BOOT_ID
        reader.close()


# ---------------------------------------------------------------- journal endpoints


def test_journal_read_and_ack(
    control_client: ControlClient, api_client: ApiClient, session: Session, schemas: SchemaSet
) -> None:
    assert control_client.request("GET", "/v1/journal").json() == {"records": [], "head": 0}
    for _ in range(3):
        api_client.append("session.hook", HOOK, SESSION_ID)
    body = control_client.request("GET", "/v1/journal?after=1&limit=1").json()
    assert schemas.errors("guest/journal.response.json", body) == []
    assert [record["seq"] for record in body["records"]] == [2]
    assert body["head"] == 3
    assert body["records"][0]["session_id"] == SESSION_ID
    assert len(control_client.request("GET", "/v1/journal").json()["records"]) == 3
    reply = control_client.request("POST", "/v1/journal/ack", {"through": 3})
    assert (reply.status, reply.json()) == (200, {})
    reply = control_client.request("POST", "/v1/journal/ack", {"through": 4})
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})


@pytest.mark.parametrize(
    "query",
    ["wait=26", "limit=0", "limit=1001", "after=-1", "after=01", "x=1", "after=1&after=2", "after", "wait=1.5"],
)
def test_journal_query_invalid(control_client: ControlClient, query: str) -> None:
    reply = control_client.request("GET", f"/v1/journal?{query}")
    assert reply.status == 400
    assert reply.json()["error"] == "request_invalid"


@pytest.mark.parametrize("body", [{"through": True}, {"through": 1.0}, {"through": -1}, {}, {"through": 1, "x": 1}])
def test_journal_ack_invalid(control_client: ControlClient, body: object) -> None:
    reply = control_client.request("POST", "/v1/journal/ack", body)
    assert (reply.status, reply.json()["error"]) == (400, "request_invalid")


def test_journal_long_poll(control_client: ControlClient, api_client: ApiClient, session: Session) -> None:
    timer = threading.Timer(0.2, api_client.append, args=("session.hook", HOOK, SESSION_ID))
    timer.start()
    started = time.monotonic()
    body = control_client.request("GET", "/v1/journal?after=0&wait=5").json()
    timer.join()
    assert [record["seq"] for record in body["records"]] == [1]
    assert time.monotonic() - started < 4
    started = time.monotonic()
    assert control_client.request("GET", "/v1/journal?after=1&wait=1").json() == {"records": [], "head": 1}
    assert time.monotonic() - started >= 1


def test_concurrent_appends_through_api_sock(
    control_client: ControlClient, api_client: ApiClient, session: Session
) -> None:
    """The API process writes only through api.sock: many threads, one gapless sequence."""
    seqs: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(25):
            seq = api_client.append("session.hook", HOOK, SESSION_ID)
            with lock:
                seqs.append(seq)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(seqs) == list(range(1, 401))
    records = control_client.request("GET", "/v1/journal?limit=1000").json()["records"]
    assert [record["seq"] for record in records] == list(range(1, 401))


# ---------------------------------------------------------------- api.sock ops of the control process


def test_hello_and_resolve(api_client: ApiClient, context: ControlContext, session: Session) -> None:
    assert api_client.hello() == {"guest_version": "0.6.0"}
    assert api_client.resolve_session(SESSION_TOKEN) == {
        "id": SESSION_ID,
        "uid": 20000,
        "state": "working",
        "workspace": f"/srv/sessions/{SESSION_ID}/work",
        "claude_session_id": None,
    }
    assert api_client.resolve_session("other") is None
    context.state.save_session(replace(session, state="stopped"))
    assert api_client.resolve_session(SESSION_TOKEN) is None
    for args in ({}, {"token_sha256": 1}, {"token_sha256": "x", "more": 1}):
        with pytest.raises(ApiSockError) as error:
            api_client.call("session.resolve", args)
        assert error.value.code == "request_invalid"


def test_resolve_compares_every_session(context: ControlContext) -> None:
    for index in range(3):
        context.state.save_session(make_session(f"01a1ee56-5e00-7003-8f1b-{index:012x}", f"t{index}"))
    found = context.state.resolve(token_sha256("t1"))
    assert found is not None
    assert found.id.endswith("000000000001")
    assert context.state.resolve("é") is None


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"kind": "session.hook", "session_id": SESSION_ID, "body": HOOK, "x": 1}, "request_invalid"),
        ({"kind": "session.output", "session_id": SESSION_ID, "body": HOOK}, "request_invalid"),
        ({"kind": "session.hook", "session_id": SESSION_ID, "body": []}, "request_invalid"),
        ({"kind": "session.hook", "session_id": OTHER_ID, "body": HOOK}, "not_found"),
        ({"kind": "session.hook", "session_id": 5, "body": HOOK}, "not_found"),
        ({"kind": "session.hook", "session_id": None, "body": HOOK}, "request_invalid"),
        ({"kind": "session.hook", "session_id": SESSION_ID, "body": {"event": "x"}}, "request_invalid"),
    ],
)
def test_append_op_refusals(api_client: ApiClient, session: Session, args: dict[str, Any], code: str) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call("journal.append", args)
    assert error.value.code == code


def test_append_op_details_and_vm_level(api_client: ApiClient, session: Session) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.append("session.hook", {"event": "PreToolUse", "decision": "nope"}, SESSION_ID)
    assert error.value.detail == ["/decision", ""]  # "": no option of the hook record or the summary
    assert api_client.append("guest.error", {"code": "gateway_route"}) == 1


def test_append_op_when_the_journal_is_closed(api_client: ApiClient, context: ControlContext, session: Session) -> None:
    context.journal.close()
    with pytest.raises(ApiSockError) as error:
        api_client.append("session.hook", HOOK, SESSION_ID)
    assert error.value.code == "unavailable"
    with pytest.raises(ApiSockError) as error:
        api_client.pending_create(SESSION_ID, "query", QUERY)
    assert error.value.code == "unavailable"


# ---------------------------------------------------------------- pending calls


def test_pending_operation_round_trip(
    control_client: ControlClient, api_client: ApiClient, session: Session, schemas: SchemaSet
) -> None:
    pending_id = api_client.pending_create(SESSION_ID, "operation", OPERATION)
    records = control_client.request("GET", "/v1/journal").json()["records"]
    assert records[0]["kind"] == "operation.request"
    assert records[0]["body"] == {**OPERATION, "pending_id": pending_id}
    assert api_client.pending_wait(pending_id, SESSION_ID, 0) is None
    answer = {"status": "ok", "result": {"operation_id": OPERATION["operation_id"], "status": "pending"}}
    assert schemas.errors("guest/pending-answer.request.json", answer) == []
    timer = threading.Timer(0.2, control_client.request, args=("POST", f"/v1/pending/{pending_id}/answer", answer))
    timer.start()
    started = time.monotonic()
    assert api_client.pending_wait(pending_id, SESSION_ID, 5) == answer
    timer.join()
    assert time.monotonic() - started < 4
    assert control_client.request("POST", f"/v1/pending/{pending_id}/answer", answer).status == 200
    reply = control_client.request("POST", f"/v1/pending/{pending_id}/answer", ANSWER_ERROR)
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})
    assert api_client.pending_wait(pending_id, SESSION_ID, 1) == answer


def test_pending_workvm_call_round_trip(
    control_client: ControlClient, api_client: ApiClient, session: Session, schemas: SchemaSet
) -> None:
    """A work VM tool call is the pending kind ``workvm``, journaled as ``workvm.call`` (KOR-29)."""
    pending_id = api_client.pending_create(SESSION_ID, "workvm", WORKVM)
    [record] = control_client.request("GET", "/v1/journal").json()["records"]
    assert (record["kind"], record["session_id"]) == ("workvm.call", SESSION_ID)
    assert record["body"] == {**WORKVM, "pending_id": pending_id}
    assert schemas.errors("guest/journal/workvm.call.json", record["body"]) == []
    answer = {"status": "error", "error": {"error": "not_found", "message": "No work VM."}}
    assert control_client.request("POST", f"/v1/pending/{pending_id}/answer", answer).status == 200
    assert api_client.pending_wait(pending_id, SESSION_ID, 1) == answer


def test_pending_workvm_call_body_is_validated(api_client: ApiClient, session: Session) -> None:
    for body in (QUERY, {"tool": "workvm_exec", "arguments": {}}, {"tool": "workvm_status", "arguments": {"x": 1}}):
        with pytest.raises(ApiSockError) as error:
            api_client.pending_create(SESSION_ID, "workvm", body)
        assert error.value.code == "request_invalid"


def test_pending_relay_is_not_for_the_api_process(api_client: ApiClient, session: Session) -> None:
    """Only the orchestrator's socket creates relay calls (05-runner.md section 17.2): api.sock refuses the kind."""
    with pytest.raises(ApiSockError) as error:
        api_client.pending_create(SESSION_ID, "relay", {"method": "GET"})
    assert error.value.code == "request_invalid"


def test_pending_query_and_wait_limit(api_client: ApiClient, context: ControlContext, session: Session) -> None:
    pending_id = api_client.pending_create(SESSION_ID, "query", QUERY)
    started = time.monotonic()
    assert api_client.pending_wait(pending_id, SESSION_ID, 0.3) is None
    assert time.monotonic() - started >= 0.3
    with pytest.raises(ApiSockError) as error:
        api_client.pending_wait(pending_id, OTHER_ID, 0)
    assert error.value.code == "not_found"
    context.pending.close()
    assert api_client.pending_wait(pending_id, SESSION_ID, 5) is None


def test_pending_answer_refusals(control_client: ControlClient, api_client: ApiClient, session: Session) -> None:
    reply = control_client.request("POST", f"/v1/pending/{OTHER_ID}/answer", ANSWER_ERROR)
    assert (reply.status, reply.json()) == (404, {"error": "not_found"})
    reply = control_client.request("POST", "/v1/pending/not-a-uuid/answer", ANSWER_ERROR)
    assert (reply.status, reply.json()) == (404, {"error": "not_found"})
    pending_id = api_client.pending_create(SESSION_ID, "query", QUERY)
    reply = control_client.request("POST", f"/v1/pending/{pending_id}/answer", {"status": "ok"})
    assert reply.status == 400
    assert reply.json()["error"] == "request_invalid"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"session_id": SESSION_ID, "kind": "query", "body": QUERY, "x": 1}, "request_invalid"),
        ({"session_id": 1, "kind": "query", "body": QUERY}, "request_invalid"),
        ({"session_id": SESSION_ID, "kind": "other", "body": QUERY}, "request_invalid"),
        ({"session_id": SESSION_ID, "kind": "query", "body": []}, "request_invalid"),
        ({"session_id": SESSION_ID, "kind": "query", "body": {**QUERY, "pending_id": OTHER_ID}}, "request_invalid"),
        ({"session_id": SESSION_ID, "kind": "query", "body": {"tool": "nope", "arguments": {}}}, "request_invalid"),
        ({"session_id": OTHER_ID, "kind": "query", "body": QUERY}, "not_found"),
    ],
)
def test_pending_create_refusals(api_client: ApiClient, session: Session, args: dict[str, Any], code: str) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call("pending.create", args)
    assert error.value.code == code


def test_pending_create_for_an_inactive_session(
    api_client: ApiClient, context: ControlContext, session: Session
) -> None:
    context.state.save_session(replace(session, state="failed"))
    with pytest.raises(ApiSockError) as error:
        api_client.pending_create(SESSION_ID, "query", QUERY)
    assert error.value.code == "not_found"


def test_pending_library_refuses_an_unknown_kind(context: ControlContext, session: Session) -> None:
    with pytest.raises(StateError) as error:
        context.pending.create(SESSION_ID, "other", QUERY)  # type: ignore[arg-type]
    assert error.value.code == "request_invalid"


@pytest.mark.parametrize(
    "args",
    [
        {"pending_id": OTHER_ID, "session_id": SESSION_ID, "timeout": 1, "x": 1},
        {"pending_id": 1, "session_id": SESSION_ID, "timeout": 1},
        {"pending_id": OTHER_ID, "session_id": 1, "timeout": 1},
        {"pending_id": OTHER_ID, "session_id": SESSION_ID, "timeout": True},
        {"pending_id": OTHER_ID, "session_id": SESSION_ID, "timeout": "1"},
        {"pending_id": OTHER_ID, "session_id": SESSION_ID, "timeout": 34},
        {"pending_id": OTHER_ID, "session_id": SESSION_ID, "timeout": -1},
    ],
)
def test_pending_wait_refusals(api_client: ApiClient, session: Session, args: dict[str, Any]) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call("pending.wait", args)
    assert error.value.code == "request_invalid"


def test_pending_wait_unknown(api_client: ApiClient, session: Session) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.pending_wait(OTHER_ID, SESSION_ID, 0)
    assert error.value.code == "not_found"


# ---------------------------------------------------------------- startup, sockets, info


def test_sockets_are_0660_with_their_group(guest_config: GuestConfig, control: ControlService, own_group: str) -> None:
    gid = grp.getgrnam(own_group).gr_gid
    for path in (guest_config.control_socket, guest_config.api_socket):
        info = os.lstat(path)
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert info.st_gid == gid


def test_stale_socket_is_replaced_and_removed_on_stop(guest_config: GuestConfig, clock: FakeClock) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(guest_config.control_socket)
    stale.close()
    service = ControlService(guest_config, clock)
    service.start()
    assert ControlClient(guest_config.control_socket).request("GET", "/v1/health").status == 200
    service.stop()
    assert not os.path.exists(guest_config.control_socket)
    assert not os.path.exists(guest_config.api_socket)


def test_startup_refusals(guest_config: GuestConfig, clock: FakeClock, tmp_path: Path) -> None:
    cases: list[tuple[GuestConfig, str]] = [
        (replace(guest_config, contracts=str(tmp_path / "none")), "contracts_missing"),
        (replace(guest_config, control_group="no-such-group-x"), "group_unknown"),
        (replace(guest_config, api_group="no-such-group-x"), "group_unknown"),
    ]
    blocker = tmp_path / "file.sock"
    blocker.write_text("", encoding="ascii")
    cases.append((replace(guest_config, api_socket=str(blocker)), "socket_path_invalid"))
    for config, code in cases:
        service = ControlService(config, clock)
        with pytest.raises(StartupError) as error:
            service.start()
        assert error.value.code == code
        assert not os.path.exists(config.control_socket)
    assert blocker.exists()


def test_startup_refuses_a_duplicate_core_op(
    guest_config: GuestConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "api_ops", lambda: {"hello": lambda context, args: {}})
    with pytest.raises(registry.RegistryError) as error:
        ControlService(guest_config, clock).start()
    assert error.value.code == "registry_duplicate"


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_load_info_defaults_and_values(guest_config: GuestConfig, tmp_path: Path) -> None:
    info = load_info(guest_config)
    assert (info.claude_version, info.landlock_abi, info.boot_id) == ("2.1.330", 6, BOOT_ID)
    bare = replace(
        guest_config, build_file=str(tmp_path / "missing.json"), claude_install_file=str(tmp_path / "missing2.json")
    )
    info = load_info(bare)
    assert (info.claude_version, info.landlock_abi) == ("unknown", 0)


@pytest.mark.parametrize(
    ("field", "content", "name"),
    [
        ("build_file", "{", "BUILD_FILE"),
        ("build_file", "[]", "BUILD_FILE"),
        ("build_file", '{"landlock_abi": true}', "BUILD_FILE"),
        ("build_file", '{"landlock_abi": 256}', "BUILD_FILE"),
        ("build_file", None, "BUILD_FILE"),
        ("claude_install_file", '{"version": "bad version"}', "CLAUDE_INSTALL_FILE"),
        ("claude_install_file", '{"version": 2}', "CLAUDE_INSTALL_FILE"),
        ("boot_id_file", "not-a-boot-id", "BOOT_ID_FILE"),
        ("boot_id_file", None, "BOOT_ID_FILE"),
    ],
)
def test_load_info_refusals(
    guest_config: GuestConfig, tmp_path: Path, field: str, content: str | None, name: str
) -> None:
    path = tmp_path / "case"
    if content is None:
        path.mkdir()
    else:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(StartupError) as error:
        load_info(replace(guest_config, **{field: str(path)}))  # type: ignore[arg-type]
    assert (error.value.code, error.value.item) == ("config_invalid", name)


def test_missing_boot_id(guest_config: GuestConfig, tmp_path: Path) -> None:
    with pytest.raises(StartupError):
        load_info(replace(guest_config, boot_id_file=str(tmp_path / "none")))


def test_prune_loop(guest_config: GuestConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control_module, "PRUNE_SECONDS", 0.05)
    service = ControlService(guest_config, clock)
    context = service.start()
    try:
        context.state.save_session(make_session())
        context.journal.append("session.hook", HOOK, SESSION_ID)
        context.journal.ack(1)
        clock.advance(25 * 3600)
        deadline = time.monotonic() + 5
        while context.journal.read(0, 10)[0] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert context.journal.read(0, 10)[0] == []
        context.journal.close()
        for thread in service._threads:
            if thread.name == "agent-guest-prune":
                thread.join(30)
        assert not [thread for thread in service._threads if thread.name == "agent-guest-prune" and thread.is_alive()]
    finally:
        service.stop()


# ---------------------------------------------------------------- entry points


@pytest.fixture
def sigterm_when_ready(guest_config: GuestConfig) -> Iterator[None]:
    """Sends SIGTERM to this process once the control socket answers (for main() in the main thread)."""

    def wait_and_signal() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if ControlClient(guest_config.control_socket).request("GET", "/v1/health").status == 200:
                    break
            except OSError:
                time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=wait_and_signal, daemon=True)
    thread.start()
    yield
    thread.join(10)


def test_main_runs_until_sigterm(guest_env: dict[str, str], sigterm_when_ready: None) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    assert main([], guest_env) == 0
    assert signal.getsignal(signal.SIGTERM) is previous
    assert not os.path.exists(guest_env["AGENT_GUEST_CONTROL_SOCKET"])


def test_a_sigterm_as_soon_as_the_socket_answers_ends_cleanly(
    guest_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handlers are installed before the service listens: a stop right after start ends with 0 (AUD-02 F5)."""
    original = control_module.ControlService.start

    def start_then_sigterm(self: ControlService) -> None:
        original(self)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(control_module.ControlService, "start", start_then_sigterm)
    previous = signal.getsignal(signal.SIGTERM)
    assert main([], guest_env) == 0
    assert signal.getsignal(signal.SIGTERM) is previous
    assert not os.path.exists(guest_env["AGENT_GUEST_CONTROL_SOCKET"])


def test_main_refusals(guest_env: dict[str, str], capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    assert main(["extra"], guest_env) == 2
    assert main([], {**guest_env, "AGENT_GUEST_TYPO": "1"}) == 1
    assert signal.getsignal(signal.SIGTERM) is previous
    assert capsys.readouterr().err.endswith("agent-guest-control: config_invalid AGENT_GUEST_TYPO\n")
    assert main([], {**guest_env, "AGENT_GUEST_CONTRACTS": str(tmp_path / "no")}) == 1
    assert "contracts_missing" in capsys.readouterr().err
    assert main([], {**guest_env, "AGENT_GUEST_JOURNAL": "/dev/null/journal.db"}) == 1
    assert "agent-guest-control: os_error" in capsys.readouterr().err


def test_main_reports_a_journal_error(
    guest_env: dict[str, str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> None:
        raise JournalError("journal_schema_mismatch")

    monkeypatch.setattr(control_module.Journal, "open", refuse)  # type: ignore[attr-defined]
    assert main([], guest_env) == 1
    # stray daemon threads of earlier tests in the same worker may write to stderr meanwhile
    assert "agent-guest-control: journal_schema_mismatch" in capsys.readouterr().err.splitlines()


def test_module_runs_as_a_process(guest_env: dict[str, str]) -> None:
    """``python3 -m agent_guest.control`` as the systemd unit starts it; SIGTERM ends it with status 0."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_GUEST_")}
    process = subprocess.Popen([sys.executable, "-m", "agent_guest.control"], env={**env, **guest_env})
    try:
        client = ControlClient(guest_env["AGENT_GUEST_CONTROL_SOCKET"])
        deadline = time.monotonic() + 20
        while True:
            try:
                assert client.request("GET", "/v1/health").status == 200
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        assert process.wait(10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cli_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(name: str) -> Any:
        def run(argv: list[str]) -> int:
            calls.append(name)
            return 0

        return run

    monkeypatch.setattr(control_module, "main", fake("control"))
    monkeypatch.setattr(server_module, "main", fake("api"))
    assert cli.main(["control"]) == 0
    assert cli.main(["api"]) == 0
    assert calls == ["control", "api"]


def test_stop_without_start(guest_config: GuestConfig, clock: FakeClock) -> None:
    service = ControlService(guest_config, clock)
    service.stop()
    assert service.stopped.is_set()
    assert not os.path.exists(guest_config.control_socket)


def test_scheme_is_case_insensitive(control_client: ControlClient) -> None:
    reply = control_client.request(
        "GET", "/v1/health", headers={"Authorization": f"bearer {CONTROL_TOKEN}"}, token=None
    )
    assert reply.status == 200


@pytest.mark.parametrize(
    "head",
    [
        b"POST /v1/nothing HTTP/1.1\r\nHost: x\r\n" + f"Authorization: Bearer {CONTROL_TOKEN}\r\n".encode(),
        b"POST /v1/assign HTTP/1.1\r\nHost: x\r\n",
    ],
)
def test_unread_body_after_an_early_error_is_never_a_request(
    guest_config: GuestConfig, control: ControlService, head: bytes
) -> None:
    smuggled = b"GET /v1/health HTTP/1.1\r\nHost: x\r\n" + auth() + b"\r\n"
    data = head + f"Content-Length: {len(smuggled)}\r\n\r\n".encode() + smuggled
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(guest_config.control_socket)
        client.sendall(data)
        answer = b""
        while chunk := client.recv(65536):
            answer += chunk
    assert answer.count(b"HTTP/1.1 ") == 1
    assert b" 200 " not in answer


@pytest.mark.parametrize(
    ("request_line", "status", "code"),
    [
        (b"HEAD /v1/health HTTP/1.1", b"404", b"not_found"),
        (b"PATCH /v1/health HTTP/1.1", b"404", b"not_found"),
        (b"GET /v1/health extra HTTP/1.1", b"400", b"request_invalid"),
    ],
)
def test_server_level_errors_are_json(
    guest_config: GuestConfig, control: ControlService, request_line: bytes, status: bytes, code: bytes
) -> None:
    answer = raw_request(guest_config.control_socket, request_line + b"\r\nHost: x\r\n" + auth() + b"\r\n")
    head, _, body = answer.partition(b"\r\n\r\n")
    assert head.split(b" ")[1] == status
    assert b"Content-Type: application/json" in head
    if request_line.startswith(b"HEAD"):
        return
    assert json.loads(body) == {"error": code.decode()}


def test_deeply_nested_json(control_client: ControlClient, api_client: ApiClient) -> None:
    reply = control_client.request("POST", "/v1/assign", raw=b"[" * 200000 + b"]" * 200000)
    assert (reply.status, reply.json()) == (400, {"error": "request_invalid"})


def test_append_op_refuses_an_ended_session(api_client: ApiClient, context: ControlContext, session: Session) -> None:
    context.state.save_session(replace(session, state="stopped"))
    with pytest.raises(ApiSockError) as error:
        api_client.append("session.hook", HOOK, SESSION_ID)
    assert error.value.code == "not_found"


def test_cli_api_does_not_load_the_journal(repo_root: Path) -> None:
    code = (
        "import sys, agent_guest.__main__;"
        "print(sorted(m for m in ('agent_guest.journal', 'agent_guest.state', 'agent_guest.control')"
        " if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=repo_root)
    assert done.stdout.strip() == "[]"


def test_closers_run_in_order_once_and_a_failure_does_not_stop_the_others(
    context: ControlContext, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def failing() -> None:
        calls.append("failing")
        raise RuntimeError("broken part")

    context.on_close(lambda: calls.append("first"))
    context.on_close(failing)
    context.on_close(lambda: calls.append("last"))
    context.close()
    context.close()
    assert calls == ["first", "failing", "last"]
    err = capsys.readouterr().err
    assert "agent-guest-control: close failed" in err
    assert "RuntimeError: broken part" in err
    # a part created while or after the process closes is closed at once
    context.on_close(lambda: calls.append("late"))
    assert calls[-1] == "late"
    assert context.closers == []


def test_stop_closes_the_parts_while_the_journal_is_still_open(guest_config: GuestConfig, clock: FakeClock) -> None:
    service = ControlService(guest_config, clock, startup=())
    context = service.start()
    try:
        context.state.save_session(make_session())
        written: list[int] = []
        context.on_close(lambda: written.append(context.journal.append("session.hook", HOOK, SESSION_ID)))
    finally:
        service.stop()
    assert len(written) == 1
    assert context.closers == []
