from __future__ import annotations

import http.client
import json
import uuid

import pytest

from agent_cli import order_ops
from agent_cli.cli_bridge import MAX_REQUEST_BYTES
from agent_cli.coordinator_common import coord
from agent_cli.dashboard_orders import (
    PREFIX,
    error_text,
    handle_order_request,
    same_origin,
)
from agent_cli.order_ops import Operation, OrderError, Param
from agent_cli.store import Store, StoreConnectionError
from test_coordinator_support import seed_order_task
from test_dashboard_control import _running_dashboard
from test_orders_cli import (
    blocked_order as seed_blocked_order,
    body_file,
    device,
    duplicate_order as seed_duplicate_question,
    order,
    order_config,
)


HOST = "127.0.0.1:7845"


def call(
    store,
    home,
    method: str,
    name: str,
    *,
    query: str = "",
    body=None,
    headers=None,
):
    request_headers = {"Host": HOST}
    if method == "POST":
        request_headers["Content-Type"] = "application/json"
    for key, value in (headers or {}).items():
        if value is None:
            request_headers.pop(key, None)
        else:
            request_headers[key] = value
    if isinstance(body, bytes):
        raw = body
    elif body is None:
        raw = b""
    else:
        raw = json.dumps(body).encode("utf-8")
    return handle_order_request(
        store,
        home,
        method=method,
        path=PREFIX + name,
        query=query,
        headers=request_headers,
        body=raw,
    )


def record_run(monkeypatch) -> list:
    calls = []

    def fake(name, store, home, params):
        calls.append((name, dict(params)))
        return {"recorded": True}

    monkeypatch.setattr(order_ops, "run", fake)
    return calls


def ledger_count(store) -> int:
    return store.conn.execute(
        "SELECT COUNT(*) AS n FROM ledger_event"
    ).fetchone()["n"]


def intent_of(device, order_id: str) -> dict:
    rows = [
        row
        for row in device.store.rows("activity")
        if row["type"] == "order.reply"
        and row["payload"]["order_id"] == order_id
    ]
    assert len(rows) == 1
    return rows[0]


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("host", "origin", "expected"),
    (
        ("127.0.0.1:7845", None, True),
        ("localhost:7845", None, True),
        ("127.0.0.1:9999", None, True),
        ("127.0.0.1:17845", "http://127.0.0.1:17845", True),
        ("localhost:17845", "http://localhost:17845", True),
        ("127.0.0.1:7845", "", True),
        ("127.0.0.1:17845", "http://127.0.0.1:7845", False),
        ("localhost:7845", "http://127.0.0.1:7845", False),
        ("127.0.0.1:7845", "https://127.0.0.1:7845", False),
        ("127.0.0.1:7845", "https://evil.example", False),
        ("127.0.0.1:7845", "null", False),
        ("evil.example:7845", None, False),
        (None, None, False),
        ("127.0.0.1", None, False),
        ("127.0.0.1:7845.evil.example", None, False),
        ("LOCALHOST:7845", None, False),
    ),
)
def test_same_origin(host, origin, expected: bool) -> None:
    headers = {}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    assert same_origin(headers) is expected


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("exc", "expected"),
    (
        (SystemExit("agent: unknown task: x"), "unknown task: x"),
        (OrderError("check a cannot be skipped"), "check a cannot be skipped"),
        (SystemExit(3), "3"),
        (SystemExit(), ""),
    ),
)
def test_error_text(exc: SystemExit, expected: str) -> None:
    assert error_text(exc) == expected


@pytest.mark.no_pg
def test_schema_needs_no_store(tmp_path) -> None:
    status, payload = call(None, tmp_path, "GET", "schema")
    assert status == 200
    assert payload["ok"] is True
    result = payload["result"]
    assert set(result) == {"enabled", "skippable", "operations"}
    assert result["enabled"] is False
    assert result["skippable"] == ["duplicates_checked"]
    assert result["operations"] == order_ops.view_operations()
    assert set(result["operations"]) == {
        "submit",
        "list",
        "show",
        "events",
        "ack",
        "reply",
        "override",
        "cancel",
        "report",
    }
    json.dumps(payload)

    status, payload = call(None, tmp_path, "POST", "schema")
    assert (status, payload) == (
        405,
        {"ok": False, "error": "method not allowed"},
    )


def test_schema_reports_whether_orders_are_enabled(device) -> None:
    status, payload = call(device.store, device.home, "GET", "schema")
    assert status == 200
    assert payload["result"]["enabled"] is True

    config = order_config(device.home)
    config["workers"]["worker-session"].pop("orders")
    (device.home / "coordinator.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    status, payload = call(device.store, device.home, "GET", "schema")
    assert status == 200
    assert payload["result"]["enabled"] is False

    (device.home / "coordinator.json").write_text("{", encoding="utf-8")
    status, payload = call(device.store, device.home, "GET", "schema")
    assert (status, payload) == (
        400,
        {"ok": False, "error": "Cannot read coordinator.json"},
    )


@pytest.mark.no_pg
def test_cross_origin_is_refused_before_anything_else(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        order_ops,
        "run",
        lambda *args, **kwargs: pytest.fail("the operation must not run"),
    )
    requests = (
        ("POST", "submit", {"repo": "example/project"}, {"Host": "evil.example:7845"}),
        ("GET", "nope", None, {"Host": "evil.example:7845"}),
        ("GET", "schema", None, {"Origin": "https://evil.example"}),
        ("POST", "list", None, {"Origin": "https://evil.example"}),
        (
            "POST",
            "submit",
            {"repo": "example/project"},
            {
                "Origin": "https://evil.example",
                "Content-Type": "text/plain",
            },
        ),
    )
    for method, name, body, headers in requests:
        status, payload = call(
            None, tmp_path, method, name, body=body, headers=headers
        )
        assert (status, payload) == (
            403,
            {"ok": False, "error": "cross-origin request refused"},
        )


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("method", "name"),
    (
        ("GET", "nope"),
        ("POST", "nope"),
        ("GET", "wait"),
        ("POST", "wait"),
        ("GET", ""),
        ("GET", "show/extra"),
    ),
)
def test_unknown_operation_and_wait(tmp_path, monkeypatch, method, name) -> None:
    monkeypatch.setattr(
        order_ops,
        "run",
        lambda *args, **kwargs: pytest.fail("the operation must not run"),
    )
    status, payload = call(None, tmp_path, method, name)
    assert (status, payload) == (
        404,
        {"ok": False, "error": "unknown order operation"},
    )


def test_every_table_operation_has_a_route(device) -> None:
    views = order_ops.view_operations()
    assert set(views) == {
        operation.name
        for operation in order_ops.OPERATIONS
        if not operation.cli_only
    }
    for name, view in views.items():
        assert view["path"].startswith(PREFIX)
        route_name = view["path"][len(PREFIX):]
        assert route_name == name
        status, payload = call(
            device.store, device.home, view["method"], route_name
        )
        required = next(
            (param for param in view["params"] if param["required"]), None
        )
        if required is None:
            assert status == 200
            assert payload["ok"] is True
        else:
            assert (status, payload) == (
                400,
                {
                    "ok": False,
                    "error": f"{required['flag']} is required",
                },
            )

        other = "POST" if view["method"] == "GET" else "GET"
        status, payload = call(device.store, device.home, other, route_name)
        assert (status, payload) == (
            405,
            {"ok": False, "error": "method not allowed"},
        )


@pytest.mark.no_pg
def test_a_new_table_row_gets_a_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        order_ops,
        "OPERATIONS",
        order_ops.OPERATIONS
        + (
            Operation(
                "probe",
                "write",
                (
                    Param("flag_value", "--flag", kind="bool"),
                    Param("text", "--text", "TEXT"),
                ),
                fields=("recorded",),
            ),
            Operation(
                "hidden", "read", (), fields=(), cli_only="test"
            ),
        ),
    )
    calls = record_run(monkeypatch)

    status, payload = call(
        None,
        tmp_path,
        "POST",
        "probe",
        body={"flag_value": True, "text": "x"},
    )
    assert status == 200
    assert payload == {"ok": True, "result": {"recorded": True}}
    assert calls[-1] == ("probe", {"flag_value": True, "text": "x"})

    for value, expected in (("0", False), ("true", True), (None, None)):
        status, _ = call(
            None,
            tmp_path,
            "POST",
            "probe",
            body={"flag_value": value},
        )
        assert status == 200
        expected_params = {} if value is None else {"flag_value": expected}
        assert calls[-1] == ("probe", expected_params)

    for value in (1, "yes"):
        before = list(calls)
        status, payload = call(
            None,
            tmp_path,
            "POST",
            "probe",
            body={"flag_value": value},
        )
        assert (status, payload) == (
            400,
            {"ok": False, "error": "flag_value must be true or false"},
        )
        assert calls == before

    assert call(None, tmp_path, "GET", "probe") == (
        405,
        {"ok": False, "error": "method not allowed"},
    )
    assert call(None, tmp_path, "GET", "hidden") == (
        404,
        {"ok": False, "error": "unknown order operation"},
    )


@pytest.mark.no_pg
def test_post_requires_json(tmp_path, monkeypatch) -> None:
    calls = record_run(monkeypatch)
    cases = (
        (
            b'{}',
            {"Content-Type": "text/plain"},
            415,
            "content type must be application/json",
        ),
        (
            b'{}',
            {"Content-Type": None},
            415,
            "content type must be application/json",
        ),
        (
            b"x" * (MAX_REQUEST_BYTES + 1),
            None,
            413,
            "request body too large",
        ),
        (b"{", None, 400, "invalid JSON"),
        (b"\xff", None, 400, "invalid JSON"),
        ([], None, 400, "body must be an object"),
    )
    for body, headers, expected_status, error in cases:
        status, payload = call(
            None,
            tmp_path,
            "POST",
            "ack",
            body=body,
            headers=headers,
        )
        assert (status, payload) == (
            expected_status,
            {"ok": False, "error": error},
        )
    assert calls == []

    status, _ = call(
        None,
        tmp_path,
        "POST",
        "ack",
        body={"event_id": "x"},
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert status == 200
    assert calls[-1] == ("ack", {"event_id": "x"})
    status, _ = call(None, tmp_path, "POST", "ack")
    assert status == 200
    assert calls[-1] == ("ack", {})


@pytest.mark.no_pg
def test_parameter_values_by_kind(tmp_path, monkeypatch) -> None:
    calls = record_run(monkeypatch)

    status, _ = call(
        None,
        tmp_path,
        "GET",
        "events",
        query="after=5&wake_only=1&unacked=false&notify_session=concierge-1",
    )
    assert status == 200
    assert calls[-1] == (
        "events",
        {
            "after": "5",
            "wake_only": True,
            "unacked": False,
            "notify_session": "concierge-1",
        },
    )

    status, _ = call(None, tmp_path, "GET", "list", query="open_only=")
    assert status == 200
    assert calls[-1] == ("list", {"open_only": False})
    status, _ = call(
        None,
        tmp_path,
        "GET",
        "list",
        query="open_only=0&open_only=1",
    )
    assert status == 200
    assert calls[-1] == ("list", {"open_only": True})
    status, payload = call(
        None, tmp_path, "GET", "list", query="open_only=yes"
    )
    assert (status, payload) == (
        400,
        {"ok": False, "error": "open_only must be true or false"},
    )

    status, _ = call(
        None,
        tmp_path,
        "POST",
        "override",
        body={
            "order_id": 12,
            "check": "duplicates_checked",
            "reason": None,
        },
    )
    assert status == 200
    assert calls[-1] == (
        "override",
        {"order_id": "12", "check": "duplicates_checked"},
    )

    skips = [{"key": "duplicates_checked", "reason": "none exists"}]
    status, _ = call(
        None,
        tmp_path,
        "POST",
        "submit",
        body={
            "repo": "example/project",
            "title": "t",
            "body": "Do it",
            "max_rounds": 3,
            "skip_checks": skips,
        },
    )
    assert status == 200
    assert calls[-1][1]["max_rounds"] == "3"
    assert calls[-1][1]["skip_checks"] == skips

    errors = (
        ({"title": ["x"]}, "title must be a string"),
        ({"body": 5}, "body must be a string"),
        ({"max_rounds": True}, "max_rounds must be a string"),
    )
    for body, error in errors:
        status, payload = call(
            None, tmp_path, "POST", "submit", body=body
        )
        assert (status, payload) == (
            400,
            {"ok": False, "error": error},
        )

    status, _ = call(
        None,
        tmp_path,
        "POST",
        "ack",
        body={"event_id": "x", "nope": 1},
    )
    assert status == 200
    assert calls[-1] == ("ack", {"event_id": "x", "nope": 1})


def test_unknown_parameter(device) -> None:
    assert call(
        device.store, device.home, "GET", "list", query="nope=1"
    ) == (400, {"ok": False, "error": "unknown parameter nope"})
    assert call(
        device.store,
        device.home,
        "POST",
        "ack",
        body={"event_id": "x", "nope": 1},
    ) == (400, {"ok": False, "error": "unknown parameter nope"})


def test_submit_list_show_round_trip(device) -> None:
    status, payload = call(
        device.store,
        device.home,
        "POST",
        "submit",
        body={
            "repo": "example/project",
            "title": "Add a widget",
            "body": "Do it",
            "skip_checks": [
                {"key": "duplicates_checked", "reason": "none exists"}
            ],
            "max_rounds": 3,
        },
    )
    assert status == 200
    result = payload["result"]
    assert result["state"] == "queued"
    assert result["worker"] == "worker-session"
    assert result["round_limit"] == {
        "max_rounds": 3,
        "origin": "submit",
        "model": "grok-model",
        "limit": 3,
    }
    assert len(result["skipped_checks"]) == 1
    assert result["skipped_checks"][0] | {"at": "ignored"} == {
        "key": "duplicates_checked",
        "reason": "none exists",
        "by": "human-owner",
        "at": "ignored",
    }
    order_id = result["order_id"]
    task = device.store.row("task", order_id)
    assert coord(task)["source"]["kind"] == "order"
    assert coord(task)["source"]["submitted_by"] == "human-owner"

    status, payload = call(
        device.store,
        device.home,
        "GET",
        "list",
        query="open_only=1",
    )
    assert status == 200
    assert [row["order_id"] for row in payload["result"]["orders"]] == [
        order_id
    ]

    status, payload = call(
        device.store,
        device.home,
        "GET",
        "show",
        query=f"order_id={order_id}",
    )
    assert status == 200
    assert payload["result"]["order_id"] == order_id
    show = next(op for op in order_ops.OPERATIONS if op.name == "show")
    assert set(payload["result"]) == set(show.fields)


@pytest.mark.parametrize(
    ("kind", "request_body", "expected"),
    (
        (
            "submit",
            {
                "repo": "bad",
                "title": "t",
                "body": "Do it",
            },
            "repo must be Owner/Name",
        ),
        (
            "submit",
            {
                "repo": "example/project",
                "title": "t",
                "body": "Do it",
                "skip_checks": [{"key": "spec_available", "reason": "x"}],
            },
            "check spec_available cannot be skipped",
        ),
        (
            "submit",
            {
                "repo": "example/project",
                "title": "t",
                "body": "Do it",
                "skip_checks": "duplicates_checked",
            },
            "skip_checks must be a list of objects with key and reason",
        ),
        (
            "submit",
            {
                "repo": "example/project",
                "title": "t",
                "body": "Do it",
                "max_rounds": 0,
            },
            "--max-rounds requires a positive integer",
        ),
        ("show", None, "unknown task"),
    ),
)
def test_refusals_use_the_command_line_texts(
    device, kind, request_body, expected
) -> None:
    if kind == "show":
        order_id = str(uuid.uuid4())
        status, payload = call(
            device.store,
            device.home,
            "GET",
            "show",
            query=f"order_id={order_id}",
        )
        expected = f"unknown task: {order_id}"
    else:
        status, payload = call(
            device.store, device.home, "POST", kind, body=request_body
        )
    assert (status, payload) == (400, {"ok": False, "error": expected})
    assert device.store.rows("task") == []


def test_unpaired_device_is_refused(device) -> None:
    device.store.set_meta("github_login", "")
    status, payload = call(
        device.store,
        device.home,
        "POST",
        "submit",
        body={
            "repo": "example/project",
            "title": "Add a widget",
            "body": "Do it",
        },
    )
    assert (status, payload) == (
        400,
        {"ok": False, "error": "device is not paired"},
    )
    assert device.store.rows("task") == []
    status, payload = call(device.store, device.home, "GET", "list")
    assert status == 200
    assert payload["ok"] is True


def test_events_and_ack(device) -> None:
    order_id = str(uuid.uuid4())
    _, event_id = seed_blocked_order(device, order_id)

    status, payload = call(
        device.store,
        device.home,
        "GET",
        "events",
        query="wake_only=1&unacked=1",
    )
    assert status == 200
    event = next(
        row for row in payload["result"]["events"] if row["id"] == event_id
    )
    assert event["wake"] is True
    assert event["acked"] is False

    status, payload = call(
        device.store,
        device.home,
        "POST",
        "ack",
        body={"event_id": event_id},
    )
    assert status == 200
    assert payload["result"] == {"event": event_id, "acked": True}
    row = device.store.row("activity", event_id)
    assert row["payload"]["ack"]["via"] == "ack"
    assert row["payload"]["ack"]["by"] == "human-owner"

    status, payload = call(
        device.store,
        device.home,
        "GET",
        "events",
        query="wake_only=1&unacked=1",
    )
    assert status == 200
    assert event_id not in {
        event["id"] for event in payload["result"]["events"]
    }

    status, payload = call(
        device.store,
        device.home,
        "POST",
        "ack",
        body={"event_id": event_id},
    )
    assert status == 200
    assert payload["result"] == {"event": event_id, "acked": True}


def _assert_matching_intents(
    route_row: dict,
    cli_row: dict,
    route_answer,
    cli_answer,
) -> None:
    assert route_row["type"] == cli_row["type"]
    assert route_row["session_id"] == cli_row["session_id"]
    assert route_row["execution_status"] == cli_row["execution_status"]
    route_payload = dict(route_row["payload"])
    cli_payload = dict(cli_row["payload"])
    for payload in (route_payload, cli_payload):
        payload.pop("order_id")
        payload.pop("answers")
        payload.pop("created_at")
    assert route_payload == cli_payload
    assert route_row["payload"]["answers"] == route_answer
    assert cli_row["payload"]["answers"] == cli_answer


def test_reply_override_cancel_insert_the_same_intents_as_the_command_line(
    device,
) -> None:
    route_id = str(uuid.uuid4())
    cli_id = str(uuid.uuid4())
    _, route_event = seed_duplicate_question(device, route_id)
    _, cli_event = seed_duplicate_question(device, cli_id)
    status, payload = call(
        device.store,
        device.home,
        "POST",
        "override",
        body={
            "order_id": route_id,
            "check": "duplicates_checked",
            "reason": "different goal",
        },
    )
    assert status == 200
    order(
        "override",
        "--id",
        cli_id,
        "--check",
        "duplicates_checked",
        "--reason",
        "different goal",
    )
    route_row = intent_of(device, route_id)
    cli_row = intent_of(device, cli_id)
    _assert_matching_intents(route_row, cli_row, route_event, cli_event)
    assert payload["result"]["intent"] == route_row["id"]

    route_id = str(uuid.uuid4())
    cli_id = str(uuid.uuid4())
    _, route_event = seed_blocked_order(device, route_id)
    _, cli_event = seed_blocked_order(device, cli_id)
    status, payload = call(
        device.store,
        device.home,
        "POST",
        "reply",
        body={"order_id": route_id, "body": "Go on"},
    )
    assert status == 200
    order(
        "reply",
        "--id",
        cli_id,
        "--body-file",
        body_file(device.home, "Go on"),
    )
    route_row = intent_of(device, route_id)
    cli_row = intent_of(device, cli_id)
    _assert_matching_intents(route_row, cli_row, route_event, cli_event)
    assert payload["result"]["intent"] == route_row["id"]

    route_id = str(uuid.uuid4())
    cli_id = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, route_id, phase="queued")
    seed_order_task(device.store, device.worker, cli_id, phase="queued")
    status, payload = call(
        device.store,
        device.home,
        "POST",
        "cancel",
        body={"order_id": route_id, "reason": "not needed"},
    )
    assert status == 200
    order("cancel", "--id", cli_id, "--reason", "not needed")
    route_row = intent_of(device, route_id)
    cli_row = intent_of(device, cli_id)
    _assert_matching_intents(route_row, cli_row, None, None)
    assert payload["result"]["intent"] == route_row["id"]


def test_report_route(device) -> None:
    status, payload = call(
        device.store,
        device.home,
        "POST",
        "submit",
        body={
            "repo": "example/project",
            "title": "Add a widget",
            "body": "Do it",
        },
    )
    assert status == 200
    order_id = payload["result"]["order_id"]

    status, payload = call(
        device.store,
        device.home,
        "GET",
        "report",
        query=f"order_id={order_id}",
    )
    assert status == 200
    report = next(op for op in order_ops.OPERATIONS if op.name == "report")
    assert set(payload["result"]) == set(report.fields)
    assert call(device.store, device.home, "GET", "report") == (
        400,
        {"ok": False, "error": "--id is required"},
    )


def test_read_routes_write_nothing(device) -> None:
    order_id = str(uuid.uuid4())
    seed_blocked_order(device, order_id)
    before = ledger_count(device.store)
    requests = (
        ("schema", ""),
        ("list", ""),
        ("show", f"order_id={order_id}"),
        ("events", ""),
        ("report", f"order_id={order_id}"),
    )
    for name, query in requests:
        status, payload = call(
            device.store,
            device.home,
            "GET",
            name,
            query=query,
        )
        assert status == 200
        assert payload["ok"] is True
    assert ledger_count(device.store) == before


@pytest.mark.no_pg
def test_store_connection_error_propagates(tmp_path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise StoreConnectionError("down")

    monkeypatch.setattr(order_ops, "run", fail)
    with pytest.raises(StoreConnectionError, match="down"):
        call(None, tmp_path, "GET", "list")


def _request(port: int, method: str, path: str, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    raw = response.read()
    content_type = response.getheader("Content-Type") or ""
    status = response.status
    connection.close()
    return status, content_type, raw


def test_order_routes_are_served_by_the_dashboard(device, monkeypatch) -> None:
    with _running_dashboard(monkeypatch) as port:
        status, content_type, raw = _request(port, "GET", "/api/orders/list")
        assert status == 200
        assert content_type.startswith("application/json")
        assert json.loads(raw) == {
            "ok": True,
            "result": {"orders": []},
        }

        status, _, raw = _request(port, "GET", "/api/orders/schema")
        payload = json.loads(raw)
        assert status == 200
        assert payload["ok"] is True
        assert len(payload["result"]["operations"]) == 9

        status, _, _ = _request(
            port,
            "GET",
            "/api/orders/list",
            headers={
                "Host": "127.0.0.1:17845",
                "Origin": "http://127.0.0.1:17845",
            },
        )
        assert status == 200

        status, _, raw = _request(
            port,
            "GET",
            "/api/orders/list",
            headers={
                "Host": "127.0.0.1:17845",
                "Origin": "http://127.0.0.1:7845",
            },
        )
        assert status == 403
        assert json.loads(raw)["error"] == "cross-origin request refused"

        status, _, raw = _request(
            port,
            "POST",
            "/api/orders/ack",
            body=json.dumps({"event_id": "x"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
        )
        assert status == 403
        assert json.loads(raw) == {
            "ok": False,
            "error": "cross-origin request refused",
        }

        status, _, raw = _request(
            port,
            "GET",
            "/api/orders/list",
            headers={"Host": "evil.example:7845"},
        )
        assert status == 403
        assert json.loads(raw)["error"] == "cross-origin request refused"

        status, _, raw = _request(port, "GET", "/api/state")
        assert status == 200
        assert "sessions" in json.loads(raw)
        status, content_type, _ = _request(port, "GET", "/")
        assert status == 200
        assert content_type.startswith("text/html")


def test_order_route_refuses_an_invalid_content_length(device, monkeypatch) -> None:
    with _running_dashboard(monkeypatch) as port:
        status, _, _ = _request(
            port, "GET", "/api/orders/list", headers={"Content-Length": "abc"}
        )
        assert status == 400


@pytest.mark.parametrize("reconnect_fails", (False, True))
def test_order_route_returns_503_on_a_lost_connection(
    device, monkeypatch, reconnect_fails: bool
) -> None:
    run_calls = []
    reconnect_calls = []
    real_reconnect = Store.reconnect

    def fail_run(*args, **kwargs):
        run_calls.append(1)
        raise StoreConnectionError("server closed the connection unexpectedly")

    def reconnect(self):
        reconnect_calls.append(1)
        if reconnect_fails:
            raise StoreConnectionError("cannot reconnect to postgres: down")
        real_reconnect(self)

    monkeypatch.setattr(order_ops, "run", fail_run)
    monkeypatch.setattr(Store, "reconnect", reconnect)

    with _running_dashboard(monkeypatch) as port:
        status, _, raw = _request(
            port,
            "POST",
            "/api/orders/ack",
            body=json.dumps({"event_id": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    assert status == 503
    payload = json.loads(raw)
    assert payload["ok"] is False
    if reconnect_fails:
        assert payload["error"] == "cannot reconnect to postgres: down"
    else:
        assert payload["error"] == "server closed the connection unexpectedly"
    assert reconnect_calls == [1]
    assert run_calls == [1]
