from __future__ import annotations

import copy
import http.client
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlencode

import pytest

from agent_cli import order_ops
from agent_cli.coordinator_checks import CheckValue, record_check
from agent_cli.coordinator_common import coord
from agent_cli.coordinator_orders import reconcile_order_events
from agent_cli.main import main
from agent_cli.order_ops import Operation
from agent_cli.preconditions import CHECKS, SKIPPABLE
from test_coordinator_support import (
    insert_order_intent,
    order_source,
    seed_order_task,
)
from test_dashboard_control import _running_dashboard
from test_dashboard_orders import call
from test_order_report import add_lane
from test_orders_cli import body_file, device, order


PAGE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent_cli"
    / "static"
    / "index.html"
)
BEGIN, END = "/* ORDER_VIEW_BEGIN */", "/* ORDER_VIEW_END */"
FORWARDED_HOST = "127.0.0.1:17845"
OPINION = {
    "result": "change-approach",
    "reasoning": "The same finding returns.",
    "approach": "Use an allowlist.",
    "limit": 2,
    "rounds": 2,
    "agent_id": None,
    "at": "2026-01-01T00:00:00Z",
}
HEAD, BASE = "a" * 40, "b" * 40


def view_of(html: str) -> dict:
    assert html.count(BEGIN) == 1
    assert html.count(END) == 1
    block = html.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    prefix = "const ORDER_VIEW = "
    assert block.startswith(prefix)
    assert block.endswith(";")
    return json.loads(block[len(prefix) : -1])


def page_view() -> dict:
    return view_of(PAGE.read_text(encoding="utf-8"))


def group_names() -> set[str]:
    with pytest.raises(SystemExit) as exc:
        main(["order"])
    text = str(exc.value)
    prefix = "Usage: agent order "
    assert prefix in text
    return set(text.split(prefix, 1)[1].split("|"))


def cli_usage(name: str) -> str:
    with pytest.raises(SystemExit) as exc:
        main(["order", name, "--no-such-flag"])
    return str(exc.value).removeprefix("agent: ")


def _operation_view(operation: Operation) -> dict:
    return {
        "mode": operation.mode,
        "method": "GET" if operation.mode == "read" else "POST",
        "path": f"/api/orders/{operation.name}",
        "params": [
            {
                "name": param.name,
                "flag": param.flag,
                "kind": param.kind,
                "required": param.required,
            }
            for param in operation.params
        ],
        "fields": list(operation.fields),
        "nested": {path: list(keys) for path, keys in operation.nested},
    }


def problems(
    operations: tuple, view: dict, cli_names: set[str]
) -> list[str]:
    found: list[str] = []
    by_name = {operation.name: operation for operation in operations}
    for name in by_name:
        if name not in cli_names:
            found.append(f"{name}: operation of the table is not a subcommand")
    for name in cli_names:
        if name not in by_name:
            found.append(f"{name}: subcommand is not in the table")

    dashboard = {
        name: operation
        for name, operation in by_name.items()
        if not operation.cli_only
    }
    for name in dashboard:
        if name not in view:
            found.append(f"{name}: missing in the page view")
    for name in view:
        if name not in dashboard:
            found.append(
                f"{name}: page view entry that the table does not offer to the dashboard"
            )

    for name in dashboard.keys() & view.keys():
        expected = _operation_view(dashboard[name])
        actual = view[name]
        for key in ("mode", "method", "path"):
            if actual.get(key) != expected[key]:
                found.append(f"{name}: {key} differs")

        expected_params = {
            item["name"]: item for item in expected["params"]
        }
        actual_params = {
            item.get("name"): item
            for item in actual.get("params", [])
            if isinstance(item, dict)
        }
        for param in expected_params.keys() - actual_params.keys():
            found.append(f"{name}: parameter {param} only in the table")
        for param in actual_params.keys() - expected_params.keys():
            found.append(f"{name}: parameter {param} only in the page")
        for param in expected_params.keys() & actual_params.keys():
            for key in ("flag", "kind", "required"):
                if actual_params[param].get(key) != expected_params[param][key]:
                    found.append(
                        f"{name}: parameter {param} differs in {key}"
                    )

        expected_fields = set(expected["fields"])
        actual_fields = set(actual.get("fields", []))
        for field in expected_fields - actual_fields:
            found.append(f"{name}: field {field} only in the table")
        for field in actual_fields - expected_fields:
            found.append(f"{name}: field {field} only in the page")

        expected_nested = expected["nested"]
        actual_nested = actual.get("nested", {})
        for path in expected_nested.keys() - actual_nested.keys():
            found.append(f"{name}: nested {path} only in the table")
        for path in actual_nested.keys() - expected_nested.keys():
            found.append(f"{name}: nested {path} only in the page")
        for path in expected_nested.keys() & actual_nested.keys():
            expected_keys = set(expected_nested[path])
            actual_keys = set(actual_nested[path])
            for key in expected_keys - actual_keys:
                found.append(
                    f"{name}: nested {path}.{key} only in the table"
                )
            for key in actual_keys - expected_keys:
                found.append(f"{name}: nested {path}.{key} only in the page")
    return sorted(found)


def nested_problems(entry: dict, result: dict) -> list[str]:
    found: list[str] = []
    expected_fields = set(entry["fields"])
    actual_fields = set(result)
    for field in expected_fields - actual_fields:
        found.append(f"field {field} only in the table")
    for field in actual_fields - expected_fields:
        found.append(f"field {field} only in the result")

    for path, keys in entry["nested"].items():
        value = result
        for segment in path.split("."):
            if value is None:
                break
            if not isinstance(value, dict) or segment not in value:
                found.append(f"nested {path} is missing")
                value = None
                break
            value = value[segment]
        if value is None or value == []:
            continue
        expected = set(keys)
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(item, dict) for item in values):
            found.append(f"nested {path} is not an object or list of objects")
            continue
        for index, item in enumerate(values):
            actual = set(item)
            suffix = f"[{index}]" if isinstance(value, list) else ""
            for key in expected - actual:
                found.append(f"nested {path}{suffix}.{key} only in the table")
            for key in actual - expected:
                found.append(f"nested {path}{suffix}.{key} only in the result")
    return sorted(found)


def route(
    device,
    name: str,
    params: dict | None = None,
    *,
    method: str | None = None,
) -> tuple[int, dict]:
    operations = order_ops.view_operations()
    if method is None:
        method = operations.get(name, {}).get("method", "GET")
    values = params or {}
    if method == "GET":
        query = urlencode(
            {
                key: "1" if value is True else value
                for key, value in values.items()
                if value is not None and value != "" and value is not False
            }
        )
        response = call(
            device.store,
            device.home,
            method,
            name,
            query=query,
            headers={"Host": FORWARDED_HOST},
        )
    else:
        response = call(
            device.store,
            device.home,
            method,
            name,
            body=values,
            headers={
                "Host": FORWARDED_HOST,
                "Origin": "http://" + FORWARDED_HOST,
            },
        )
    assert response[0] != 403
    return response


def seed_parity_order(device) -> str:
    order_id = str(uuid.uuid4())
    task = seed_order_task(
        device.store,
        device.worker,
        order_id,
        phase="ask",
        resume_phase="implement",
        failed_check="round_budget",
        question_text="Round limit reached",
        accepted_at="2026-01-01T00:00:00Z",
        pr_number=42,
        head_sha=HEAD,
        base_sha=BASE,
        source=order_source(
            order_id,
            skipped_checks=[
                {
                    "key": "duplicates_checked",
                    "reason": "none exists",
                    "by": "human-owner",
                    "at": "2026-01-01T00:00:00Z",
                }
            ],
        ),
        round_limit={
            "max_rounds": 2,
            "origin": "submit",
            "model": "grok-model",
            "limit": 2,
        },
        round_limit_question={
            "limit": 2,
            "replies": 0,
            "assessment": dict(OPINION),
        },
        change_stats={
            "head": HEAD,
            "base": BASE,
            "total": 1,
            "added": 3,
            "deleted": 1,
            "files": [{"path": "src/a.py", "added": 3, "deleted": 1}],
            "omitted": 0,
        },
    )
    record_check(
        device.store,
        task,
        CHECKS["duplicates_checked"],
        CheckValue(
            "ja",
            "skipped at submit by human-owner: none exists",
            source="human",
        ),
        action="open_draft",
        skip={
            "by": "human-owner",
            "reason": "none exists",
            "at": "2026-01-01T00:00:00Z",
        },
    )
    record_check(
        device.store,
        task,
        CHECKS["round_budget"],
        CheckValue(
            "nein",
            "round 3 exceeds the limit of 2 implementation rounds; "
            "assessment change-approach",
            opinion=dict(OPINION),
        ),
        action="implement",
    )
    check_id = str(uuid.uuid4())
    device.store.write(
        "local_check",
        "insert",
        check_id,
        {
            "id": check_id,
            "task_id": order_id,
            "name": "coordinator-check",
            "command": "/operator/checks --full",
            "result": "pass",
            "output": "ok",
            "ran_at": "2026-01-01T00:00:00Z",
            "head_sha": HEAD,
        },
    )
    add_lane(
        device.store,
        task,
        str(uuid.uuid4()),
        role="reviewer",
        started="2026-01-01T00:00:00Z",
        finished="2026-01-01T00:01:00Z",
        round=1,
    )
    reconcile_order_events(
        device.store,
        device.worker,
        device.store.row("task", order_id),
    )
    event_id = coord(device.store.row("task", order_id))["order"]["checkpoint"][
        "event_id"
    ]
    insert_order_intent(
        device.store,
        device.worker,
        order_id,
        "reply",
        body="Go on.",
        answers=event_id,
    )
    return order_id


@pytest.mark.no_pg
def test_front_ends_offer_the_same_operations_and_fields() -> None:
    assert problems(
        order_ops.OPERATIONS, page_view(), group_names()
    ) == []


@pytest.mark.no_pg
def test_cli_only_operations_are_pinned() -> None:
    assert {
        operation.name
        for operation in order_ops.OPERATIONS
        if operation.cli_only
    } == {"wait"}
    assert "wait" not in page_view()


@pytest.mark.no_pg
def test_page_view_equals_the_schema_route(tmp_path) -> None:
    status, payload = call(
        None,
        tmp_path,
        "GET",
        "schema",
        headers={"Host": FORWARDED_HOST},
    )
    assert status == 200
    assert page_view() == payload["result"]["operations"]
    assert payload["result"]["operations"] == order_ops.view_operations()
    assert list(page_view()) == list(order_ops.view_operations())


@pytest.mark.no_pg
def test_schema_route_keys_are_pinned(tmp_path) -> None:
    status, payload = call(
        None,
        tmp_path,
        "GET",
        "schema",
        headers={"Host": FORWARDED_HOST},
    )
    assert status == 200
    result = payload["result"]
    assert set(result) == {"enabled", "skippable", "operations"}
    assert result["skippable"] == sorted(SKIPPABLE)
    table_names = set()
    for operation in order_ops.OPERATIONS:
        table_names.update(operation.fields)
        for path, keys in operation.nested:
            table_names.add(path)
            table_names.update(keys)
    assert {"enabled", "skippable"}.isdisjoint(table_names)


@pytest.mark.no_pg
def test_every_parameter_is_a_command_line_flag() -> None:
    for operation in order_ops.OPERATIONS:
        usage = cli_usage(operation.name)
        assert usage.startswith(f"Usage: agent order {operation.name}")
        tokens = {word.strip("[].") for word in usage.split()}
        for param in operation.params:
            assert param.flag in tokens
        assert ("--json" in tokens) == operation.json_flag
        allowed = {param.flag for param in operation.params}
        allowed.update(
            word
            for param in operation.params
            for word in param.meta.split()
            if word.startswith("--")
        )
        if operation.json_flag:
            allowed.add("--json")
        assert {token for token in tokens if token.startswith("--")} <= allowed


@pytest.mark.no_pg
def test_the_page_calls_every_operation() -> None:
    html = PAGE.read_text(encoding="utf-8")
    for name in page_view():
        assert f'orderCall("{name}"' in html
    called = set(re.findall(r'orderCall\("([a-z_]+)"', html))
    assert called <= set(page_view())


@pytest.mark.no_pg
def test_rendering_is_generic_over_the_table() -> None:
    html = PAGE.read_text(encoding="utf-8")
    outside = html.split(BEGIN, 1)[0] + html.split(END, 1)[1]
    names = (
        "pr_url",
        "updated_at",
        "last_event",
        "last_intent",
        "failed_kind",
        "round_limit",
        "skipped_checks",
        "summary_en",
        "reviews_omitted",
        "local_checks",
        "a38_report",
        "blocks_merge",
        "observed_at",
        "exit_code",
    )
    view = page_view()
    declared = set()
    for entry in view.values():
        declared.update(entry["fields"])
        declared.update(entry["nested"])
        for keys in entry["nested"].values():
            declared.update(keys)
    for name in names:
        assert name in declared
        assert name not in outside


def test_every_page_operation_is_served(device) -> None:
    for name, entry in page_view().items():
        status, _payload = route(device, name, {})
        assert status in (200, 400)
        other = "POST" if entry["method"] == "GET" else "GET"
        status, _payload = route(device, name, {}, method=other)
        assert status == 405
    status, payload = route(device, "wait", {})
    assert (status, payload) == (
        404,
        {"ok": False, "error": "unknown order operation"},
    )


def test_read_operations_return_the_same_fields_in_both_front_ends(
    device, capsys
) -> None:
    order_id = seed_parity_order(device)
    cases = (
        ("list", [], {}),
        ("list", ["--open"], {"open_only": True}),
        ("show", ["--id", order_id], {"order_id": order_id}),
        ("events", [], {}),
        (
            "events",
            ["--wake-only", "--unacked"],
            {"wake_only": True, "unacked": True},
        ),
        ("events", ["--after", "2"], {"after": 2}),
        ("report", ["--id", order_id], {"order_id": order_id}),
    )
    results = {}
    for name, argv, params in cases:
        order(name, *argv, "--json")
        printed = json.loads(capsys.readouterr().out.strip())
        status, payload = route(device, name, params)
        assert status == 200
        result = json.loads(json.dumps(payload["result"]))
        operation = next(
            item for item in order_ops.OPERATIONS if item.name == name
        )
        expected = result[operation.json_root] if operation.json_root else result
        assert printed == expected
        assert nested_problems(order_ops.view_operations()[name], result) == []
        results.setdefault(name, []).append(result)

    shown = results["show"][0]
    for field in ("last_event", "checkpoint", "last_intent", "round_limit", "assessment"):
        assert isinstance(shown[field], dict)
    for field in ("checks", "overridable_checks", "skipped_checks"):
        assert shown[field]
    assert len(results["events"][0]["events"]) == 3
    assert isinstance(results["events"][0]["events"][-1]["opinion"], dict)
    assert len(results["events"][1]["events"]) == 1
    assert len(results["events"][2]["events"]) == 1
    report = results["report"][0]
    for field in ("rounds", "files", "ci"):
        assert isinstance(report[field], dict)
    assert report["waivers"]
    assert report["files"]["listed"]
    assert report["reviews"]
    assert report["local_checks"]


def test_write_operations_reach_the_same_function(
    device, monkeypatch, capsys
) -> None:
    recorded = []
    canned = {
        "submit": {
            "order_id": "o-1",
            "repo": "example/project",
            "state": "queued",
            "worker": "worker-session",
            "round_limit": None,
            "skipped_checks": [],
        },
        "ack": {"event": "e-1", "acked": True},
        "reply": {"intent": "i-1", "order_id": "o-1"},
        "override": {
            "intent": "i-1",
            "order_id": "o-1",
            "check": "duplicates_checked",
        },
        "cancel": {"intent": "i-1", "order_id": "o-1"},
    }

    def fake_run(name, store, home, params):
        recorded.append((name, dict(params)))
        return canned[name]

    monkeypatch.setattr(order_ops, "run", fake_run)
    path = body_file(device.home)
    cases = (
        (
            "submit",
            [
                "--repo",
                "example/project",
                "--title",
                "Add a widget",
                "--body-file",
                path,
                "--skip-check",
                "duplicates_checked",
                "--reason",
                "none exists",
                "--max-rounds",
                "3",
            ],
            {
                "repo": "example/project",
                "title": "Add a widget",
                "body": "Do it",
                "skip_checks": [
                    {"key": "duplicates_checked", "reason": "none exists"}
                ],
                "max_rounds": "3",
            },
        ),
        ("ack", ["--event", "e-1"], {"event_id": "e-1"}),
        (
            "reply",
            ["--id", "o-1", "--body-file", path],
            {"order_id": "o-1", "body": "Do it"},
        ),
        (
            "override",
            [
                "--id",
                "o-1",
                "--check",
                "duplicates_checked",
                "--reason",
                "different goal",
            ],
            {
                "order_id": "o-1",
                "check": "duplicates_checked",
                "reason": "different goal",
            },
        ),
        (
            "cancel",
            ["--id", "o-1", "--reason", "not needed"],
            {"order_id": "o-1", "reason": "not needed"},
        ),
    )
    for index, (name, argv, params) in enumerate(cases):
        order(name, *argv)
        capsys.readouterr()
        status, _payload = route(device, name, params)
        assert status == 200
        left, right = recorded[index * 2 : index * 2 + 2]
        if name == "submit":
            left = (left[0], {**left[1], "max_rounds": str(left[1]["max_rounds"])})
            right = (
                right[0],
                {**right[1], "max_rounds": str(right[1]["max_rounds"])},
            )
        assert left == right
    assert {name for name, _params in recorded} == {
        operation.name
        for operation in order_ops.OPERATIONS
        if operation.mode == "write"
    }


def test_error_texts_are_the_same_in_both_front_ends(device) -> None:
    order_id = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, order_id, phase="queued")
    unknown = str(uuid.uuid4())
    cases = (
        (
            "show",
            ["--id", unknown],
            {"order_id": unknown},
        ),
        ("show", [], {}),
        (
            "submit",
            [
                "--repo",
                "example/project",
                "--title",
                "Add a widget",
                "--body-file",
                body_file(device.home),
                "--skip-check",
                "spec_available",
                "--reason",
                "x",
            ],
            {
                "repo": "example/project",
                "title": "Add a widget",
                "body": "Do it",
                "skip_checks": [{"key": "spec_available", "reason": "x"}],
            },
        ),
        (
            "submit",
            [
                "--repo",
                "example/project",
                "--title",
                "Add a widget",
                "--body-file",
                body_file(device.home),
                "--max-rounds",
                "0",
            ],
            {
                "repo": "example/project",
                "title": "Add a widget",
                "body": "Do it",
                "max_rounds": "0",
            },
        ),
        (
            "cancel",
            ["--id", order_id, "--reason", "a\nb"],
            {"order_id": order_id, "reason": "a\nb"},
        ),
    )
    for name, argv, params in cases:
        with pytest.raises(SystemExit) as exc:
            order(name, *argv)
        cli_error = str(exc.value).removeprefix("agent: ")
        status, payload = route(device, name, params)
        assert status == 400
        assert payload["error"] == cli_error
    assert len(device.store.rows("task")) == 1
    assert not any(
        row.get("type") == "order.reply"
        for row in device.store.rows("activity")
    )


def test_the_served_page_carries_the_table_of_its_routes(
    device, monkeypatch
) -> None:
    with _running_dashboard(monkeypatch) as port:
        assert port != 7845
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert BEGIN in body

        connection.request("GET", "/api/orders/schema")
        response = connection.getresponse()
        schema = json.loads(response.read())
        assert response.status == 200
        assert schema["ok"] is True
        assert view_of(body) == schema["result"]["operations"]

        connection.request(
            "GET",
            "/api/orders/list",
            headers={"Host": FORWARDED_HOST},
        )
        response = connection.getresponse()
        listed = json.loads(response.read())
        connection.close()
        assert response.status == 200
        assert listed == {"ok": True, "result": {"orders": []}}


@pytest.mark.no_pg
def test_parity_check_detects_differences() -> None:
    operations = order_ops.OPERATIONS
    view = order_ops.view_operations()
    names = {operation.name for operation in operations}
    assert problems(operations, view, names) == []

    extra = operations + (Operation("extra", "read", (), fields=()),)
    result = problems(extra, view, names)
    assert "extra: operation of the table is not a subcommand" in result
    assert "extra: missing in the page view" in result

    without_report = tuple(
        operation for operation in operations if operation.name != "report"
    )
    result = problems(without_report, view, names)
    assert "report: subcommand is not in the table" in result
    assert (
        "report: page view entry that the table does not offer to the dashboard"
        in result
    )

    changed = copy.deepcopy(view)
    changed["show"]["fields"].append("extra")
    assert "show: field extra only in the page" in problems(
        operations, changed, names
    )

    changed = copy.deepcopy(view)
    changed["report"]["fields"].remove("ci")
    assert "report: field ci only in the table" in problems(
        operations, changed, names
    )

    changed = copy.deepcopy(view)
    changed["submit"]["params"].append(
        {
            "name": "extra",
            "flag": "--extra",
            "kind": "line",
            "required": False,
        }
    )
    assert "submit: parameter extra only in the page" in problems(
        operations, changed, names
    )

    changed = copy.deepcopy(view)
    next(
        param
        for param in changed["submit"]["params"]
        if param["name"] == "max_rounds"
    )["kind"] = "line"
    assert "submit: parameter max_rounds differs in kind" in problems(
        operations, changed, names
    )

    changed = copy.deepcopy(view)
    changed["events"]["nested"]["events"].append("extra")
    assert "events: nested events.extra only in the page" in problems(
        operations, changed, names
    )

    changed = copy.deepcopy(view)
    changed["wait"] = copy.deepcopy(changed["events"])
    assert (
        "wait: page view entry that the table does not offer to the dashboard"
        in problems(operations, changed, names)
    )

    assert "extra: subcommand is not in the table" in problems(
        operations, view, names | {"extra"}
    )

    changed = copy.deepcopy(view)
    changed["list"]["method"] = "POST"
    assert "list: method differs" in problems(operations, changed, names)
