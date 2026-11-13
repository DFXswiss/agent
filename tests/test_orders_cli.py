from __future__ import annotations

import inspect
import json
import re
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import order_ops, orders
from agent_cli.coordinator import tick
from agent_cli.coordinator_checks import CheckValue, record_check, registry_row_id
from agent_cli.coordinator_common import coord, redact, save_task
from agent_cli.coordinator_config import load_coordinator_config
from agent_cli.coordinator_orders import (
    own_order_events,
    post_order_event,
    record_submitted_skips,
    reconcile_order_events,
)
from agent_cli.main import main
from agent_cli.preconditions import CHECKS
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    insert_order_intent,
    make_session,
    order_lane_runner,
    order_source,
    patch_account_runners,
    scan_done,
    seed_order_task,
    submit_order,
    write_accounts,
    write_check_lane,
)


BASE = (
    "submit",
    "--repo",
    "example/project",
    "--title",
    "t",
    "--body-file",
)

OPINION = {
    "result": "change-approach",
    "reasoning": "The same finding returns.",
    "approach": "Use an allowlist.",
    "limit": 2,
    "rounds": 2,
    "agent_id": None,
    "at": "2026-01-01T00:00:00Z",
}


def order_config(root: Path) -> dict:
    return {
        "workers": {
            "worker-session": {
                "review_session": "review-session",
                "workspace_root": str(root / "work"),
                "repositories": {
                    "example/project": {
                        "base": "develop",
                        "publication_repo": "example/project",
                        "check_argv": ["/operator/checks", "--full"],
                        "readiness_argv": ["/operator/readiness"],
                    }
                },
                "reply_logins": ["human-owner"],
                "poll_seconds": 30,
                "lane_timeout": 60,
                "check_timeout": 30,
                "park_poll_seconds": 0,
                "lane_retries": 0,
                "orders": True,
            }
        }
    }


@pytest.fixture
def device(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    (tmp_path / "work").mkdir()
    store = Store(tmp_path)
    store.set_meta("github_login", "Human-Owner")
    write_accounts(tmp_path)
    write_check_lane(tmp_path)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    (tmp_path / "coordinator.json").write_text(
        json.dumps(order_config(tmp_path)), encoding="utf-8"
    )
    worker = load_coordinator_config(tmp_path)["worker-session"]
    yield SimpleNamespace(home=tmp_path, store=store, worker=worker)
    store.close()


def order(*argv: str) -> None:
    main(["order", *argv])


def body_file(root: Path, text: str = "Do it") -> str:
    path = root / "order.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def blocked_order(device, order_id: str) -> tuple[dict, str]:
    task = seed_order_task(
        device.store,
        device.worker,
        order_id,
        phase="blocked",
        resume_phase="ci",
        blocker="CI needs a decision",
    )
    reconcile_order_events(device.store, device.worker, task)
    task = device.store.row("task", order_id)
    event_id = task["payload"]["coordinator"]["order"]["checkpoint"]["event_id"]
    return task, event_id


def duplicate_order(device, order_id: str) -> tuple[dict, str]:
    task = seed_order_task(
        device.store,
        device.worker,
        order_id,
        phase="ask",
        resume_phase="start_draft",
        failed_check="duplicates_checked",
        question_text="Check duplicates_checked failed",
        blocker="Check duplicates_checked failed",
    )
    record_check(
        device.store,
        task,
        CHECKS["duplicates_checked"],
        CheckValue("nein", "open pull request #5 looks like the same work"),
        action="open_draft",
    )
    reconcile_order_events(device.store, device.worker, task)
    task = device.store.row("task", order_id)
    event_id = task["payload"]["coordinator"]["order"]["checkpoint"]["event_id"]
    return task, event_id


@pytest.mark.no_pg
def test_group_usage() -> None:
    assert orders.USAGE == (
        "Usage: agent order submit|list|show|events|wait|ack|reply|override|cancel"
    )
    for argv in (("order",), ("order", "nope")):
        with pytest.raises(SystemExit) as exc:
            main(list(argv))
        assert orders.USAGE in str(exc.value)


USAGE_LINES = {
    "submit": "Usage: agent order submit --repo OWNER/NAME --title TEXT --body-file PATH [--base BRANCH] [--worker SESSION] [--notify-session ID] [--skip-check KEY --reason TEXT]... [--max-rounds N] [--json]",
    "list": "Usage: agent order list [--open] [--json]",
    "show": "Usage: agent order show --id UUID [--json]",
    "events": "Usage: agent order events [--after CURSOR] [--notify-session ID] [--wake-only] [--unacked] [--json]",
    "wait": "Usage: agent order wait [--after CURSOR] [--notify-session ID] [--timeout SECONDS]",
    "ack": "Usage: agent order ack --event ID",
    "reply": "Usage: agent order reply --id UUID --body-file PATH",
    "override": "Usage: agent order override --id UUID --check KEY --reason TEXT",
    "cancel": "Usage: agent order cancel --id UUID --reason TEXT",
}


@pytest.mark.no_pg
@pytest.mark.parametrize("name", tuple(USAGE_LINES))
def test_usage_of_a_subcommand_is_generated(name: str) -> None:
    with pytest.raises(SystemExit) as exc:
        order(name, "--nope")
    assert str(exc.value) == f"agent: {USAGE_LINES[name]}"


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("argv", "text"),
    (
        (("show", "--id=abc"), USAGE_LINES["show"]),
        (("show", "--id", "a", "--id", "b"), USAGE_LINES["show"]),
        (("ack", "--event", "x", "--json"), USAGE_LINES["ack"]),
        (("list", "stray"), USAGE_LINES["list"]),
        (("show", "--id"), "--id needs a value"),
    ),
)
def test_option_forms(monkeypatch, argv: tuple[str, ...], text: str) -> None:
    from agent_cli import main as cli

    monkeypatch.setattr(
        cli,
        "open_store",
        lambda: pytest.fail("the store must not be opened for syntax errors"),
    )
    with pytest.raises(SystemExit) as exc:
        order(*argv)
    assert text in str(exc.value)


@pytest.mark.no_pg
def test_operations_table_is_consistent() -> None:
    names = [operation.name for operation in order_ops.OPERATIONS]
    assert names == [
        "submit",
        "list",
        "show",
        "events",
        "wait",
        "ack",
        "reply",
        "override",
        "cancel",
    ]
    assert len(names) == len(set(names))
    for operation in order_ops.OPERATIONS:
        if operation.func is None:
            continue
        parameters = list(inspect.signature(operation.func).parameters)
        expected = ["store", "home", *(["by"] if operation.identity else [])]
        expected.extend(param.name for param in operation.params)
        assert parameters == expected
    assert [op.name for op in order_ops.OPERATIONS if op.func is None] == ["wait"]
    assert [op.name for op in order_ops.OPERATIONS if op.cli_only] == ["wait"]
    assert {op.name for op in order_ops.OPERATIONS if op.identity} == {
        op.name for op in order_ops.OPERATIONS if op.mode == "write"
    }
    assert {op.name for op in order_ops.OPERATIONS if op.json_flag} == {
        "submit",
        "list",
        "show",
        "events",
    }
    views = order_ops.view_operations()
    assert list(views) == [name for name in names if name != "wait"]
    for name, view in views.items():
        assert set(view) == {"mode", "method", "path", "params", "fields", "nested"}
        assert (view["method"] == "GET") == (view["mode"] == "read")
        assert view["path"] == f"/api/orders/{name}"
    assert set(orders.RENDER) == set(names) - {"wait"}


@pytest.mark.no_pg
def test_assign_still_validates_through_the_shared_function(tmp_path) -> None:
    from agent_cli import main as cli

    with pytest.raises(SystemExit, match="repo must be Owner/Name"):
        main(
            [
                "assign",
                "--no-deviation",
                "--agent",
                "a",
                "--repo",
                "bad",
                "--title",
                "t",
                "--body",
                "b",
            ]
        )
    with pytest.raises(SystemExit, match="body must be 1..60000 characters"):
        cli._read_request_text("x/y", "t", "", None)
    with pytest.raises(SystemExit, match="cannot read body file"):
        cli._read_body_file(str(tmp_path / "missing"))


def test_run_refuses_unknown_names_and_parameters(device) -> None:
    with pytest.raises(order_ops.OrderError, match="unknown order operation"):
        order_ops.run("nope", device.store, device.home, {})
    with pytest.raises(order_ops.OrderError, match="unknown order operation"):
        order_ops.run("wait", device.store, device.home, {})
    values = {"order_id": "x", "extra": 1}
    with pytest.raises(order_ops.OrderError, match="unknown parameter extra"):
        order_ops.run("show", device.store, device.home, values)
    assert values == {"order_id": "x", "extra": 1}
    with pytest.raises(order_ops.OrderError, match="--id is required"):
        order_ops.run("show", device.store, device.home, {})
    with pytest.raises(order_ops.OrderError, match="--reason is required"):
        order_ops.run(
            "cancel",
            device.store,
            device.home,
            {"order_id": "x", "reason": ""},
        )


def test_submit_prints_the_order_id(device, capsys) -> None:
    order(
        "submit",
        "--repo",
        "example/project",
        "--title",
        "Add a widget",
        "--body-file",
        body_file(device.home),
    )
    output = capsys.readouterr().out.strip()
    assert re.fullmatch(r"order [0-9a-f-]{36}", output)
    order_id = output.split()[1]
    task = device.store.row("task", order_id)
    checkpoint = task["payload"]["coordinator"]
    assert checkpoint["phase"] == "queued"
    assert checkpoint["source"]["submitted_by"] == "human-owner"

    order(
        "submit",
        "--repo",
        "example/project",
        "--title",
        "Another widget",
        "--body-file",
        body_file(device.home, "Another request"),
        "--notify-session",
        "concierge-1",
        "--json",
    )
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {
        "order_id",
        "repo",
        "state",
        "worker",
        "round_limit",
        "skipped_checks",
    }
    assert result | {"order_id": "ignored"} == {
        "order_id": "ignored",
        "repo": "example/project",
        "state": "queued",
        "worker": "worker-session",
        "round_limit": {
            "limit": 10,
            "max_rounds": 10,
            "model": "grok-model",
            "origin": "default",
        },
        "skipped_checks": [],
    }
    task = device.store.row("task", result["order_id"])
    assert task["payload"]["coordinator"]["source"]["notify_session"] == "concierge-1"


def test_submit_picks_the_least_loaded_worker(device, capsys) -> None:
    config_path = device.home / "coordinator.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    second = json.loads(json.dumps(config["workers"]["worker-session"]))
    second["workspace_root"] = str(device.home / "work-b")
    second["review_session"] = "review-session"
    config["workers"]["another-session"] = second
    (device.home / "work-b").mkdir()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    make_session(device.store, "another-session", ["spine", "review-loop", "pr-review"])

    github_path = device.home / "github-accounts.json"
    github = json.loads(github_path.read_text(encoding="utf-8"))
    github["sessions"]["another-session"] = "worker"
    github_path.write_text(json.dumps(github), encoding="utf-8")
    ai_path = device.home / "ai-accounts.json"
    ai = json.loads(ai_path.read_text(encoding="utf-8"))
    ai["sessions"]["another-session"] = json.loads(
        json.dumps(ai["sessions"]["worker-session"])
    )
    ai_path.write_text(json.dumps(ai), encoding="utf-8")

    def submit(title: str, *extra: str) -> dict:
        order(
            "submit",
            "--repo",
            "example/project",
            "--title",
            title,
            "--body-file",
            body_file(device.home, title),
            *extra,
            "--json",
        )
        return json.loads(capsys.readouterr().out)

    assert submit("First")["worker"] == "another-session"
    assert submit("Second")["worker"] == "worker-session"
    assert submit("Third", "--worker", "worker-session")["worker"] == "worker-session"


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--repo", "bad", "--title", "t"), "repo must be Owner/Name"),
        (("--title", "t"), "--repo is required"),
        (("--repo", "example/project", "--title", ""), "--title is required"),
        (("--repo", "example/project", "--title", "x" * 257), "title must be 1..256 characters"),
        (("--repo", "example/project", "--title", "two\nlines"), "title must be one line"),
        (("--repo", "example/project", "--title", "t", "--base", "bad..branch"), "invalid base branch"),
        (("--repo", "other/thing", "--title", "t"), "no order-enabled worker serves other/thing"),
        (("--repo", "example/project", "--title", "t", "--worker", "missing"), "coordinator session is not configured"),
        (("--repo", "other/thing", "--title", "t", "--worker", "worker-session"), "worker worker-session does not serve other/thing"),
        (("--repo", "example/project", "--title", "t", "--base", "main"), "base main differs from the configured base develop of example/project"),
    ),
)
def test_submit_errors(device, arguments: tuple[str, ...], message: str) -> None:
    before = list(device.store.rows("task"))
    with pytest.raises(SystemExit) as exc:
        order("submit", *arguments, "--body-file", body_file(device.home))
    assert message in str(exc.value)
    assert device.store.rows("task") == before


def test_submit_body_and_non_order_worker_errors(device) -> None:
    missing = device.home / "missing-order.md"
    with pytest.raises(SystemExit, match="cannot read body file"):
        order(
            "submit",
            "--repo",
            "example/project",
            "--title",
            "Missing body",
            "--body-file",
            str(missing),
        )
    with pytest.raises(SystemExit, match="body must be 1..60000 characters"):
        order(
            "submit",
            "--repo",
            "example/project",
            "--title",
            "Empty body",
            "--body-file",
            body_file(device.home, ""),
        )

    path = device.home / "coordinator.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    plain = json.loads(json.dumps(config["workers"]["worker-session"]))
    plain["workspace_root"] = str(device.home / "plain-work")
    plain["orders"] = False
    (device.home / "plain-work").mkdir()
    config["workers"]["plain-session"] = plain
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(
        SystemExit, match="worker plain-session does not accept orders"
    ):
        order(
            "submit",
            "--repo",
            "example/project",
            "--title",
            "Plain worker",
            "--body-file",
            body_file(device.home),
            "--worker",
            "plain-session",
        )
    assert device.store.rows("task") == []


def test_submit_requires_a_paired_device(device) -> None:
    device.store.set_meta("github_login", "")
    with pytest.raises(SystemExit, match="device is not paired"):
        order(
            "submit",
            "--repo",
            "bad",
            "--title",
            "t",
            "--body-file",
            body_file(device.home),
        )
    assert device.store.rows("task") == []


def test_submit_with_a_skipped_check_and_a_round_limit(device, capsys) -> None:
    path = body_file(device.home)
    order(
        *BASE,
        path,
        "--skip-check",
        "duplicates_checked",
        "--reason",
        "none exists",
        "--max-rounds",
        "4",
        "--json",
    )
    result = json.loads(capsys.readouterr().out)
    assert result["round_limit"] == {
        "max_rounds": 4,
        "origin": "submit",
        "model": "grok-model",
        "limit": 4,
    }
    task = device.store.row("task", result["order_id"])
    source = coord(task)["source"]
    assert result["skipped_checks"] == [
        {
            "key": "duplicates_checked",
            "reason": "none exists",
            "by": "human-owner",
            "at": source["submitted_at"],
        }
    ]
    assert coord(task)["round_limit"] == result["round_limit"]
    assert source["skipped_checks"] == result["skipped_checks"]
    assert device.store.rows("checklist_item") == []

    order(
        *BASE,
        path,
        "--skip-check",
        "duplicates_checked",
        "--reason",
        "none exists",
        "--max-rounds",
        "4",
    )
    assert re.fullmatch(
        r"order [0-9a-f-]{36}\n?", capsys.readouterr().out
    )


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("extra", "message"),
    (
        (
            ("--skip-check", "duplicates_checked"),
            "--skip-check KEY must be followed by --reason TEXT",
        ),
        (
            ("--skip-check", "duplicates_checked", "--reason"),
            "--skip-check KEY must be followed by --reason TEXT",
        ),
        (
            ("--skip-check", "duplicates_checked", "--max-rounds", "4"),
            "--skip-check KEY must be followed by --reason TEXT",
        ),
        (("--reason", "x"), "--reason must follow --skip-check KEY"),
        (
            (
                "--skip-check",
                "duplicates_checked",
                "--reason",
                "x",
                "--reason",
                "y",
            ),
            "--reason must follow --skip-check KEY",
        ),
        (
            ("--skip-check=duplicates_checked", "--reason", "x"),
            USAGE_LINES["submit"],
        ),
        (("--max-rounds=4",), USAGE_LINES["submit"]),
        (
            ("--max-rounds", "4", "--max-rounds", "5"),
            USAGE_LINES["submit"],
        ),
        (("--max-rounds",), "--max-rounds needs a value"),
    ),
)
def test_submit_pair_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
    message: str,
) -> None:
    from agent_cli import main as cli

    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(
        cli,
        "open_store",
        lambda: pytest.fail("the store must not be opened for syntax errors"),
    )
    with pytest.raises(SystemExit) as exc:
        order(*BASE, "unused.md", *extra)
    assert message in str(exc.value)


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        (("--skip-check", "nope", "--reason", "x"), "unknown check nope"),
        (
            ("--skip-check", "spec_available", "--reason", "x"),
            "check spec_available cannot be skipped",
        ),
        (
            (
                "--skip-check",
                "duplicates_checked",
                "--reason",
                "x",
                "--skip-check",
                "duplicates_checked",
                "--reason",
                "y",
            ),
            "check duplicates_checked is skipped twice",
        ),
        (
            ("--skip-check", "duplicates_checked", "--reason", "a\nb"),
            "reason must be one line of 1..500 characters",
        ),
        (
            ("--skip-check", "duplicates_checked", "--reason", ""),
            "reason must be one line of 1..500 characters",
        ),
        (
            ("--skip-check", "duplicates_checked", "--reason", "   "),
            "reason must be one line of 1..500 characters",
        ),
        (
            ("--skip-check", "duplicates_checked", "--reason", "x" * 501),
            "reason must be one line of 1..500 characters",
        ),
    ),
)
def test_submit_skip_errors(
    device, extra: tuple[str, ...], message: str
) -> None:
    with pytest.raises(SystemExit, match=re.escape(message)):
        order(*BASE, body_file(device.home), *extra)
    assert device.store.rows("task") == []
    control = device.worker.workspace_root / ".coordinator-control"
    assert list(control.glob("*/task-text.md")) == []


@pytest.mark.parametrize("value", ("0", "-1", "x", "1.5", "+3"))
def test_submit_max_rounds_errors(device, value: str) -> None:
    with pytest.raises(
        SystemExit, match="--max-rounds requires a positive integer"
    ):
        order(
            *BASE,
            body_file(device.home),
            "--max-rounds",
            value,
        )
    assert device.store.rows("task") == []


def test_submit_max_rounds_is_not_capped(device, capsys) -> None:
    order(
        *BASE,
        body_file(device.home),
        "--max-rounds",
        "1000",
        "--json",
    )
    result = json.loads(capsys.readouterr().out)
    assert result["round_limit"]["limit"] == 1000
    assert result["round_limit"]["max_rounds"] == 1000
    assert result["round_limit"]["origin"] == "submit"


def test_submit_values_of_another_front_end(device) -> None:
    base = {
        "repo": "example/project",
        "title": "t",
        "body": "Do it",
    }
    malformed = (
        "duplicates_checked",
        [{"key": "duplicates_checked"}],
        [{"key": "duplicates_checked", "reason": "x", "by": "someone"}],
        [{"key": 5, "reason": "x"}],
    )
    for value in malformed:
        with pytest.raises(
            order_ops.OrderError,
            match="skip_checks must be a list of objects with key and reason",
        ):
            order_ops.run(
                "submit",
                device.store,
                device.home,
                {**base, "skip_checks": value},
            )
    for value in (True, 1.5, 0):
        with pytest.raises(
            order_ops.OrderError,
            match="--max-rounds requires a positive integer",
        ):
            order_ops.run(
                "submit",
                device.store,
                device.home,
                {**base, "max_rounds": value},
            )
    assert device.store.rows("task") == []

    result = order_ops.run(
        "submit",
        device.store,
        device.home,
        {**base, "skip_checks": [], "max_rounds": 3},
    )
    assert result["round_limit"]["limit"] == 3
    assert result["round_limit"]["origin"] == "submit"
    assert result["skipped_checks"] == []
    task = device.store.row("task", result["order_id"])
    assert "skipped_checks" not in coord(task)["source"]


def test_show_prints_the_round_limit_and_the_skips(device, capsys) -> None:
    order(
        *BASE,
        body_file(device.home),
        "--skip-check",
        "duplicates_checked",
        "--reason",
        "none exists",
        "--max-rounds",
        "4",
        "--json",
    )
    submitted = json.loads(capsys.readouterr().out)
    order_id = submitted["order_id"]
    order("show", "--id", order_id)
    lines = capsys.readouterr().out.splitlines()
    head_index = next(i for i, line in enumerate(lines) if line.startswith("head "))
    assert lines[head_index + 1] == "round-limit 4 per-grant 4 origin submit"
    assert lines[head_index + 2] == (
        "skipped duplicates_checked by human-owner: none exists"
    )
    order("show", "--id", order_id, "--json")
    shown = json.loads(capsys.readouterr().out)
    assert shown["round_limit"] == submitted["round_limit"]
    assert shown["skipped_checks"] == submitted["skipped_checks"]

    task = device.store.row("task", order_id)
    record_submitted_skips(device.store, task)
    coord(task)["phase"] = "start_draft"
    coord(task)["round_limit"]["limit"] = 8
    save_task(device.store, task)
    order("show", "--id", order_id)
    text = capsys.readouterr().out
    assert "round-limit 8 per-grant 4 origin submit" in text
    assert (
        "check duplicates_checked ja skipped at submit by human-owner: none exists"
        in text
    )
    order("show", "--id", order_id, "--json")
    shown = json.loads(capsys.readouterr().out)
    checks = {item["key"]: item for item in shown["checks"]}
    assert checks["duplicates_checked"] == {
        "key": "duplicates_checked",
        "status": "ja",
        "source": "human",
        "evidence": "skipped at submit by human-owner: none exists",
    }
    assert checks["order_active"]["source"] == "script"


def test_show_round_limit_names_the_model(device, capsys) -> None:
    config = order_config(device.home)
    config["workers"]["worker-session"]["max_rounds_by_model"] = {
        "grok-model": 3
    }
    (device.home / "coordinator.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    order(*BASE, body_file(device.home))
    order_id = capsys.readouterr().out.split()[1]
    order("show", "--id", order_id)
    assert (
        "round-limit 3 per-grant 3 origin model grok-model"
        in capsys.readouterr().out
    )


def test_show_without_a_pin_or_a_waiver(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(
        device.store, device.worker, order_id, phase="implement"
    )
    before = deepcopy(task)
    order("show", "--id", order_id)
    lines = capsys.readouterr().out.splitlines()
    assert "round-limit -" in lines
    assert not any(line.startswith("skipped ") for line in lines)
    order("show", "--id", order_id, "--json")
    shown = json.loads(capsys.readouterr().out)
    assert shown["round_limit"] is None
    assert shown["skipped_checks"] == []
    assert device.store.row("task", order_id) == before


@pytest.mark.no_pg
def test_operations_table_declares_the_new_parameters() -> None:
    submit = next(op for op in order_ops.OPERATIONS if op.name == "submit")
    show = next(op for op in order_ops.OPERATIONS if op.name == "show")
    assert [param.name for param in submit.params[-2:]] == [
        "skip_checks",
        "max_rounds",
    ]
    assert submit.params[-2].flag == "--skip-check"
    assert submit.params[-2].kind == "skips"
    assert submit.params[-2].required is False
    assert submit.params[-1].flag == "--max-rounds"
    assert submit.params[-1].kind == "int"
    assert submit.params[-1].required is False
    assert submit.fields[-2:] == ("round_limit", "skipped_checks")
    assert show.fields[-3:] == (
        "round_limit",
        "skipped_checks",
        "assessment",
    )

    views = order_ops.view_operations()
    expected = {
        "round_limit": ["max_rounds", "origin", "model", "limit"],
        "skipped_checks": ["key", "reason", "by", "at"],
    }
    assert views["submit"]["nested"] == expected
    assert views["show"]["nested"]["checks"] == [
        "key",
        "status",
        "source",
        "evidence",
    ]
    for key, value in expected.items():
        assert views["show"]["nested"][key] == value


def test_list_and_show_are_bounded(device, capsys) -> None:
    order("list")
    assert capsys.readouterr().out.strip() == "order list none"
    marker = "BODY-MARKER-DO-NOT-PRINT"
    first = submit_order(device.store, device.worker, text=marker)
    ended_id = str(uuid.uuid4())
    ended = seed_order_task(device.store, device.worker, ended_id, phase="done")
    ended["state"] = "done"
    save_task(device.store, ended)
    post_order_event(
        device.store,
        device.worker,
        first,
        kind="accepted",
        occurrence="bounded",
        digest="DIGEST-MARKER-DO-NOT-PRINT",
    )

    order("list")
    text = capsys.readouterr().out
    assert len(text.strip().splitlines()) == 2
    assert marker not in text
    order("list", "--open")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1
    order("list", "--json")
    listed = json.loads(capsys.readouterr().out)
    assert all(len(item) == 9 for item in listed)

    order("show", "--id", str(first["id"]))
    text = capsys.readouterr().out
    assert [line.split()[0] for line in text.splitlines()[:10]] == [
        "order",
        "state",
        "repo",
        "pr",
        "head",
        "round-limit",
        "last-event",
        "checkpoint",
        "last-answer",
        "action",
    ]
    assert marker not in text and "DIGEST-MARKER" not in text
    assert not any(line.startswith("assessment ") for line in text.splitlines())
    order("show", "--id", str(first["id"]), "--json")
    text = capsys.readouterr().out
    assert marker not in text and "DIGEST-MARKER" not in text


def test_show_lists_checks_of_the_current_action(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, order_id, phase="start_draft")
    before = {
        table: device.store.rows(table)
        for table in ("task", "checklist_item", "activity")
    }
    order("show", "--id", order_id)
    lines = capsys.readouterr().out.splitlines()
    assert "action open_draft" in lines
    check_lines = [line for line in lines if line.startswith("check ")]
    assert check_lines[0].startswith("check order_active ja ")
    assert check_lines[1:] == [
        "check checkout_owned pending -",
        "check duplicates_checked pending -",
    ]
    order("show", "--id", order_id, "--json")
    result = json.loads(capsys.readouterr().out)
    assert [item["key"] for item in result["checks"]] == [
        "order_active",
        "checkout_owned",
        "duplicates_checked",
    ]
    assert before == {
        table: device.store.rows(table)
        for table in ("task", "checklist_item", "activity")
    }


def test_show_in_ask_uses_the_resume_phase(device, capsys) -> None:
    asking = str(uuid.uuid4())
    seed_order_task(
        device.store,
        device.worker,
        asking,
        phase="ask",
        resume_phase="start_draft",
    )
    order("show", "--id", asking)
    assert "action open_draft" in capsys.readouterr().out
    queued = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, queued, phase="queued")
    order("show", "--id", queued)
    text = capsys.readouterr().out
    assert "action -" in text
    assert not any(line.startswith("check ") for line in text.splitlines())


def test_show_unknown_and_foreign_ids(device) -> None:
    missing = str(uuid.uuid4())
    with pytest.raises(SystemExit, match="unknown task:"):
        order("show", "--id", missing)
    issue_id = str(uuid.uuid4())
    task = seed_order_task(device.store, device.worker, issue_id, phase="queued")
    task["payload"]["coordinator"]["source"]["kind"] = "github-issue"
    save_task(device.store, task)
    with pytest.raises(SystemExit, match=f"task {issue_id} is not an order"):
        order("show", "--id", issue_id)


def test_show_prints_the_pending_checkpoint_even_when_acknowledged(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    _, event_id = blocked_order(device, order_id)
    order("show", "--id", order_id)
    assert f"checkpoint 1 {event_id} blocked unacked answers reply,cancel" in capsys.readouterr().out
    order("ack", "--event", event_id)
    capsys.readouterr()
    order("show", "--id", order_id)
    assert f"checkpoint 1 {event_id} blocked acked answers reply,cancel" in capsys.readouterr().out
    order("events", "--wake-only", "--unacked")
    assert capsys.readouterr().out.strip() == "order events none"
    order("show", "--id", order_id, "--json")
    result = json.loads(capsys.readouterr().out)
    assert set(result["checkpoint"]) == {
        "event_id",
        "seq",
        "kind",
        "check",
        "acked",
        "answers",
    }


def test_show_prints_the_last_answer(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    _, event_id = blocked_order(device, order_id)
    order("show", "--id", order_id)
    assert "last-answer -" in capsys.readouterr().out
    intent_id = insert_order_intent(
        device.store,
        device.worker,
        order_id,
        "reply",
        body="x",
        answers=event_id,
    )
    order("show", "--id", order_id)
    assert "last-answer reply pending -" in capsys.readouterr().out
    row = device.store.row("activity", intent_id)
    row["execution_status"] = "error"
    row["execution_error"] = "check duplicates_checked needs an override"
    device.store.write("activity", "update", intent_id, row)
    order("show", "--id", order_id)
    assert (
        "last-answer reply error check duplicates_checked needs an override"
        in capsys.readouterr().out
    )


def test_show_lists_the_overridable_checks(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(device.store, device.worker, order_id, phase="start_draft")
    order("show", "--id", order_id)
    assert "overridable duplicates_checked pending" in capsys.readouterr().out
    record_check(
        device.store,
        task,
        CHECKS["duplicates_checked"],
        CheckValue("nein", "duplicate"),
        action="open_draft",
    )
    order("show", "--id", order_id)
    assert "overridable duplicates_checked nein" in capsys.readouterr().out


def test_events_filters(device, capsys) -> None:
    first_id = str(uuid.uuid4())
    first = seed_order_task(device.store, device.worker, first_id, phase="blocked")
    second_id = str(uuid.uuid4())
    second = seed_order_task(
        device.store,
        device.worker,
        second_id,
        phase="blocked",
        source=order_source(second_id, notify_session="concierge-1"),
    )
    accepted = post_order_event(
        device.store,
        device.worker,
        first,
        kind="accepted",
        occurrence="first",
        digest="accepted",
    )
    post_order_event(
        device.store,
        device.worker,
        second,
        kind="question",
        occurrence="second",
        digest="question",
    )
    order("ack", "--event", accepted)
    capsys.readouterr()

    order("events", "--json")
    result = json.loads(capsys.readouterr().out)
    assert [item["seq"] for item in result["events"]] == [1, 2]
    assert all(len(item) == 16 for item in result["events"])
    order("events", "--after", "1", "--json")
    assert [item["seq"] for item in json.loads(capsys.readouterr().out)["events"]] == [2]
    order("events", "--notify-session", "concierge-1", "--json")
    assert [item["seq"] for item in json.loads(capsys.readouterr().out)["events"]] == [2]
    order("events", "--wake-only", "--json")
    assert [item["seq"] for item in json.loads(capsys.readouterr().out)["events"]] == [2]
    order("events", "--unacked", "--json")
    assert [item["seq"] for item in json.loads(capsys.readouterr().out)["events"]] == [2]
    order("events", "--after", "9", "--json")
    result = json.loads(capsys.readouterr().out)
    assert result == {"cursor": 9, "events": []}
    with pytest.raises(SystemExit, match="--after requires a non-negative integer"):
        order("events", "--after", "x")


def test_events_text_line_carries_the_event_id(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(device.store, device.worker, order_id, phase="blocked")
    event_id = post_order_event(
        device.store,
        device.worker,
        task,
        kind="blocked",
        occurrence="line",
        digest="first line\nsecond line",
    )
    order("events")
    line = capsys.readouterr().out.strip()
    assert line.split()[2] == event_id
    assert "first line" in line and "second line" not in line
    order("ack", "--event", line.split()[2])
    assert capsys.readouterr().out.strip() == f"acked {event_id}"


def test_wait_returns_on_a_wake_event(device, capsys, monkeypatch) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(device.store, device.worker, order_id, phase="ask")
    post_order_event(
        device.store,
        device.worker,
        task,
        kind="question",
        occurrence="wait",
        digest="answer needed",
    )
    monkeypatch.setattr(
        orders,
        "time",
        SimpleNamespace(
            monotonic=lambda: 0.0,
            sleep=lambda _: pytest.fail("wait slept despite a ready event"),
        ),
    )
    order("wait")
    result = json.loads(capsys.readouterr().out)
    assert [event["kind"] for event in result["events"]] == ["question"]


def test_wait_ignores_acknowledged_and_non_wake_events(device, capsys, monkeypatch) -> None:
    first_id = str(uuid.uuid4())
    first = seed_order_task(device.store, device.worker, first_id, phase="queued")
    post_order_event(
        device.store,
        device.worker,
        first,
        kind="accepted",
        occurrence="accepted",
        digest="accepted",
    )
    second_id = str(uuid.uuid4())
    second = seed_order_task(device.store, device.worker, second_id, phase="blocked")
    blocked = post_order_event(
        device.store,
        device.worker,
        second,
        kind="blocked",
        occurrence="blocked",
        digest="blocked",
    )
    order("ack", "--event", blocked)
    capsys.readouterr()
    values = iter((0.0, 0.4, 0.8, 1.2))
    sleeps: list[float] = []
    monkeypatch.setattr(
        orders,
        "time",
        SimpleNamespace(monotonic=lambda: next(values), sleep=sleeps.append),
    )
    with pytest.raises(SystemExit) as exc:
        order("wait", "--timeout", "1")
    assert exc.value.code == 3
    assert json.loads(capsys.readouterr().out) == {"cursor": 0, "events": []}
    assert len(sleeps) == 2
    assert all(0 < value <= 0.6 for value in sleeps)


def test_wait_requires_an_order_worker(device) -> None:
    path = device.home / "coordinator.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["workers"]["worker-session"]["orders"] = False
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match="orders are not enabled on this device"):
        order("wait")
    for value in ("0", "x"):
        with pytest.raises(SystemExit, match="--timeout requires a positive integer"):
            order("wait", "--timeout", value)


def test_ack_is_idempotent(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(device.store, device.worker, order_id, phase="blocked")
    event_id = post_order_event(
        device.store,
        device.worker,
        task,
        kind="blocked",
        occurrence="ack",
        digest="blocked",
    )
    order("ack", "--event", event_id)
    assert capsys.readouterr().out.strip() == f"acked {event_id}"
    first = device.store.row("activity", event_id)["payload"]["ack"]
    assert first["by"] == "human-owner" and first["via"] == "ack"
    order("ack", "--event", event_id)
    assert capsys.readouterr().out.strip() == f"acked {event_id}"
    assert device.store.row("activity", event_id)["payload"]["ack"]["at"] == first["at"]
    with pytest.raises(SystemExit, match="order event missing not found"):
        order("ack", "--event", "missing")
    other_id = insert_order_intent(
        device.store, device.worker, order_id, "reply", body="x"
    )
    with pytest.raises(SystemExit, match=f"order event {other_id} not found"):
        order("ack", "--event", other_id)


def test_reply_inserts_an_intent_and_acknowledges_the_checkpoint(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task, event_id = blocked_order(device, order_id)
    unchanged = device.store.row("task", order_id)
    body = "Reply with\ncontrols\x1b[31m"
    order(
        "reply",
        "--id",
        order_id,
        "--body-file",
        body_file(device.home, body),
    )
    output = capsys.readouterr().out
    intent_id = output.split()[1]
    row = device.store.row("activity", intent_id)
    assert output == f"reply {intent_id} order {order_id}\n"
    assert row["session_id"] == "worker-session"
    assert row["execution_status"] == "pending"
    assert row["payload"] | {"created_at": "ignored"} == {
        "order_id": order_id,
        "kind": "reply",
        "body": redact(body, limit=2000),
        "by": "human-owner",
        "answers": event_id,
        "created_at": "ignored",
    }
    intents = [
        item
        for item in device.store.activity_of_types(("order.reply",))
        if item["payload"].get("order_id") == order_id
    ]
    assert [item["id"] for item in intents] == [intent_id]
    ack = device.store.row("activity", event_id)["payload"]["ack"]
    assert ack["via"] == "reply"
    assert device.store.row("task", order_id) == unchanged
    assert task["id"] == order_id


def test_reply_needs_a_pending_checkpoint(device) -> None:
    queued = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, queued, phase="queued")
    with pytest.raises(SystemExit, match="has no pending question or resumable blocker"):
        order("reply", "--id", queued, "--body-file", body_file(device.home))
    ended = str(uuid.uuid4())
    ended_task, _ = blocked_order(device, ended)
    ended_task["state"] = "failed"
    save_task(device.store, ended_task)
    with pytest.raises(
        SystemExit,
        match=f"order {ended} has no pending question or resumable blocker",
    ):
        order("reply", "--id", ended, "--body-file", body_file(device.home))
    blocked = str(uuid.uuid4())
    blocked_order(device, blocked)
    with pytest.raises(SystemExit, match="body must be 1..60000 characters"):
        order("reply", "--id", blocked, "--body-file", body_file(device.home, ""))
    assert not any(row.get("type") == "order.reply" for row in device.store.rows("activity"))


def test_reply_is_refused_for_a_duplicate(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task, _ = duplicate_order(device, order_id)
    with pytest.raises(SystemExit, match="use agent order override or agent order cancel"):
        order("reply", "--id", order_id, "--body-file", body_file(device.home))
    record_check(
        device.store,
        task,
        CHECKS["duplicates_checked"],
        CheckValue("unavailable", "resolver unavailable"),
        action="open_draft",
    )
    order("reply", "--id", order_id, "--body-file", body_file(device.home))
    assert capsys.readouterr().out.startswith("reply ")


def test_override_inserts_an_intent(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    _, event_id = duplicate_order(device, order_id)
    reason = "The reported pull request is unrelated"
    order(
        "override",
        "--id",
        order_id,
        "--check",
        "duplicates_checked",
        "--reason",
        reason,
    )
    output = capsys.readouterr().out
    intent_id = output.split()[1]
    row = device.store.row("activity", intent_id)
    assert output == (
        f"override {intent_id} order {order_id} check duplicates_checked\n"
    )
    assert row["payload"]["kind"] == "override"
    assert row["payload"]["check"] == "duplicates_checked"
    assert row["payload"]["reason"] == reason
    assert row["payload"]["answers"] == event_id
    intents = [
        item
        for item in device.store.activity_of_types(("order.reply",))
        if item["payload"].get("order_id") == order_id
    ]
    assert [item["id"] for item in intents] == [intent_id]
    assert device.store.row(
        "checklist_item", registry_row_id(order_id, "duplicates_checked")
    )["status"] == "nein"
    assert device.store.row("activity", event_id)["payload"]["ack"]["via"] == "override"


@pytest.mark.parametrize(
    ("check", "reason", "message"),
    (
        ("nope", "because", "unknown check nope"),
        ("spec_available", "because", "check spec_available cannot be overridden"),
        ("duplicates_checked", "two\nlines", "reason must be one line of 1..500 characters"),
        ("duplicates_checked", "x" * 501, "reason must be one line of 1..500 characters"),
    ),
)
def test_override_errors(device, check: str, reason: str, message: str) -> None:
    order_id = str(uuid.uuid4())
    duplicate_order(device, order_id)
    with pytest.raises(SystemExit) as exc:
        order(
            "override",
            "--id",
            order_id,
            "--check",
            check,
            "--reason",
            reason,
        )
    assert message in str(exc.value)
    assert not any(
        row.get("type") == "order.reply" for row in device.store.rows("activity")
    )


def test_override_state_errors(device) -> None:
    terminal_id = str(uuid.uuid4())
    terminal, _ = duplicate_order(device, terminal_id)
    terminal["state"] = "failed"
    terminal["payload"]["coordinator"]["failed_kind"] = "cancelled"
    save_task(device.store, terminal)
    with pytest.raises(SystemExit, match=f"order {terminal_id} is already cancelled"):
        order(
            "override",
            "--id",
            terminal_id,
            "--check",
            "duplicates_checked",
            "--reason",
            "No longer relevant",
        )

    pending_id = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, pending_id, phase="start_draft")
    with pytest.raises(
        SystemExit,
        match=f"check duplicates_checked is not failed on order {pending_id}",
    ):
        order(
            "override",
            "--id",
            pending_id,
            "--check",
            "duplicates_checked",
            "--reason",
            "No row",
        )

    yes_id = str(uuid.uuid4())
    yes = seed_order_task(device.store, device.worker, yes_id, phase="start_draft")
    record_check(
        device.store,
        yes,
        CHECKS["duplicates_checked"],
        CheckValue("ja", "No duplicate"),
        action="open_draft",
    )
    with pytest.raises(
        SystemExit,
        match=f"check duplicates_checked is not failed on order {yes_id}",
    ):
        order(
            "override",
            "--id",
            yes_id,
            "--check",
            "duplicates_checked",
            "--reason",
            "Already passed",
        )
    with pytest.raises(SystemExit, match="--reason is required"):
        order(
            "override",
            "--id",
            terminal_id,
            "--check",
            "duplicates_checked",
            "--reason",
            "",
        )
    assert not any(
        row.get("type") == "order.reply" for row in device.store.rows("activity")
    )


def test_cancel_inserts_an_intent(device, capsys) -> None:
    queued = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, queued, phase="queued")
    queued_reason = "No longer needed"
    order("cancel", "--id", queued, "--reason", queued_reason)
    output = capsys.readouterr().out
    intent_id = output.split()[1]
    intent = device.store.row("activity", intent_id)
    assert output == f"cancel {intent_id} order {queued}\n"
    assert intent["payload"]["kind"] == "cancel"
    assert intent["payload"]["reason"] == queued_reason
    assert intent["payload"]["answers"] is None
    queued_intents = [
        item
        for item in device.store.activity_of_types(("order.reply",))
        if item["payload"].get("order_id") == queued
    ]
    assert [item["id"] for item in queued_intents] == [intent_id]

    blocked = str(uuid.uuid4())
    _, event_id = blocked_order(device, blocked)
    blocked_reason = "Stop this work"
    order("cancel", "--id", blocked, "--reason", blocked_reason)
    output = capsys.readouterr().out
    intent_id = output.split()[1]
    intent = device.store.row("activity", intent_id)
    assert output == f"cancel {intent_id} order {blocked}\n"
    assert intent["payload"]["reason"] == blocked_reason
    assert intent["payload"]["answers"] == event_id
    blocked_intents = [
        item
        for item in device.store.activity_of_types(("order.reply",))
        if item["payload"].get("order_id") == blocked
    ]
    assert [item["id"] for item in blocked_intents] == [intent_id]
    assert device.store.row("activity", event_id)["payload"]["ack"]["via"] == "cancel"


def test_cancel_refuses_a_terminal_order(device) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(device.store, device.worker, order_id, phase="done")
    task["state"] = "done"
    save_task(device.store, task)
    with pytest.raises(SystemExit, match=f"order {order_id} is already merged"):
        order("cancel", "--id", order_id, "--reason", "Stop")
    assert not any(row.get("type") == "order.reply" for row in device.store.rows("activity"))


def test_results_carry_exactly_the_declared_fields(device) -> None:
    submitted = order_ops.run(
        "submit",
        device.store,
        device.home,
        {
            "repo": "example/project",
            "title": "Fields",
            "body": "Do it",
        },
    )
    order_id = submitted["order_id"]
    task = device.store.row("task", order_id)
    event_id = post_order_event(
        device.store,
        device.worker,
        task,
        kind="accepted",
        occurrence="fields",
        digest="accepted",
    )
    results = {
        "submit": submitted,
        "list": order_ops.run("list", device.store, device.home, {}),
        "show": order_ops.run(
            "show", device.store, device.home, {"order_id": order_id}
        ),
        "events": order_ops.run("events", device.store, device.home, {}),
        "ack": order_ops.run(
            "ack", device.store, device.home, {"event_id": event_id}
        ),
    }
    reply_id = str(uuid.uuid4())
    blocked_order(device, reply_id)
    results["reply"] = order_ops.run(
        "reply",
        device.store,
        device.home,
        {"order_id": reply_id, "body": "Answer"},
    )
    override_id = str(uuid.uuid4())
    duplicate_order(device, override_id)
    results["override"] = order_ops.run(
        "override",
        device.store,
        device.home,
        {
            "order_id": override_id,
            "check": "duplicates_checked",
            "reason": "The pull request is unrelated",
        },
    )
    cancel_id = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, cancel_id, phase="queued")
    results["cancel"] = order_ops.run(
        "cancel",
        device.store,
        device.home,
        {"order_id": cancel_id, "reason": "No longer needed"},
    )
    assert set(results) == {
        "submit",
        "list",
        "show",
        "events",
        "ack",
        "reply",
        "override",
        "cancel",
    }
    for name, result in results.items():
        operation = next(op for op in order_ops.OPERATIONS if op.name == name)
        assert set(result) == set(operation.fields)
        for path, keys in operation.nested:
            value = result[path]
            if value is None:
                continue
            items = value if isinstance(value, list) else [value]
            assert all(set(item) == set(keys) for item in items)


def test_commands_start_no_process(device, capsys, monkeypatch) -> None:
    queued = str(uuid.uuid4())
    seed_order_task(device.store, device.worker, queued, phase="queued")
    blocked = str(uuid.uuid4())
    _, event_id = blocked_order(device, blocked)
    duplicate = str(uuid.uuid4())
    duplicate_order(device, duplicate)
    with monkeypatch.context() as patch:
        patch.setattr(
            subprocess,
            "Popen",
            lambda *args, **kwargs: pytest.fail("an order command started a process"),
        )
        order(
            "submit",
            "--repo",
            "example/project",
            "--title",
            "No process",
            "--body-file",
            body_file(device.home),
        )
        order("list")
        order("show", "--id", queued)
        order("events")
        order("reply", "--id", blocked, "--body-file", body_file(device.home))
        order(
            "override",
            "--id",
            duplicate,
            "--check",
            "duplicates_checked",
            "--reason",
            "Not a duplicate",
        )
        order("cancel", "--id", queued, "--reason", "Stop")
        order("ack", "--event", event_id)
    capsys.readouterr()


def test_submit_then_tick_then_events_round_trip(device, capsys, monkeypatch) -> None:
    order(
        "submit",
        "--repo",
        "example/project",
        "--title",
        "Round trip",
        "--body-file",
        body_file(device.home),
    )
    capsys.readouterr()
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    from agent_cli import github_act

    monkeypatch.setattr(github_act, "scan_github", scan_done)
    tick(
        device.store,
        device.worker,
        runner=fake,
        lane_runner=order_lane_runner(fake),
    )
    order("events", "--json")
    result = json.loads(capsys.readouterr().out)
    assert result["events"][0] | {"id": "ignored", "order_id": "ignored", "at": "ignored"} == {
        "id": "ignored",
        "seq": 1,
        "order_id": "ignored",
        "kind": "accepted",
        "wake": False,
        "digest": "Order accepted for example/project.",
        "pr_url": None,
        "phase": "checkout",
        "round": 0,
        "check": None,
        "failed_kind": None,
        "notify_session": None,
        "at": "ignored",
        "acked": False,
        "answers": [],
        "opinion": None,
    }
    order_id = result["events"][0]["order_id"]
    order("show", "--id", order_id)
    assert "last-event 1 accepted" in capsys.readouterr().out


def test_show_prints_the_assessment(device, capsys) -> None:
    order_id = str(uuid.uuid4())
    task = seed_order_task(
        device.store,
        device.worker,
        order_id,
        phase="ask",
        resume_phase="implement",
        failed_check="round_budget",
        question_text="Round limit reached",
        round_limit={
            "max_rounds": 2,
            "origin": "submit",
            "model": "grok-model",
            "limit": 2,
        },
        round_limit_question={
            "limit": 2,
            "replies": 0,
            "assessment": OPINION,
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
            opinion=OPINION,
        ),
        action="implement",
    )

    order("show", "--id", order_id)
    lines = capsys.readouterr().out.splitlines()
    assessment = (
        "assessment change-approach at-limit 2: "
        "The same finding returns."
    )
    assert assessment in lines
    assert "approach Use an allowlist." in lines
    assert lines.index(assessment) > max(
        index
        for index, line in enumerate(lines)
        if line.startswith("overridable ")
    )

    order("show", "--id", order_id, "--json")
    shown = json.loads(capsys.readouterr().out)
    assert set(shown["assessment"]) == {
        "result",
        "reasoning",
        "approach",
        "limit",
        "rounds",
        "at",
    }

    reconcile_order_events(device.store, device.worker, task)
    order("events", "--json")
    events = json.loads(capsys.readouterr().out)["events"]
    event = next(item for item in events if item["order_id"] == order_id)
    assert event["kind"] == "question"
    assert event["opinion"] == {
        "result": "change-approach",
        "reasoning": "The same finding returns.",
        "approach": "Use an allowlist.",
    }

    other_id = str(uuid.uuid4())
    seed_order_task(
        device.store,
        device.worker,
        other_id,
        phase="ask",
        resume_phase="implement",
        failed_check="round_budget",
        question_text="Round limit reached",
    )
    order("show", "--id", other_id)
    assert not any(
        line.startswith("assessment ")
        for line in capsys.readouterr().out.splitlines()
    )
    order("show", "--id", other_id, "--json")
    assert json.loads(capsys.readouterr().out)["assessment"] is None


def test_show_prints_an_unavailable_assessment_without_an_approach(
    device, capsys
) -> None:
    order_id = str(uuid.uuid4())
    opinion = {
        **OPINION,
        "result": "unavailable",
        "reasoning": "check lane failed 1 times: exit 2",
        "approach": "",
    }
    task = seed_order_task(
        device.store,
        device.worker,
        order_id,
        phase="ask",
        resume_phase="implement",
        failed_check="round_budget",
        question_text="Round limit reached",
    )
    record_check(
        device.store,
        task,
        CHECKS["round_budget"],
        CheckValue("nein", "round limit reached", opinion=opinion),
        action="implement",
    )

    order("show", "--id", order_id)
    lines = capsys.readouterr().out.splitlines()
    assert (
        "assessment unavailable at-limit 2: "
        "check lane failed 1 times: exit 2"
    ) in lines
    assert not any(line.startswith("approach ") for line in lines)


@pytest.mark.no_pg
def test_operations_table_declares_the_assessment_fields() -> None:
    show = next(op for op in order_ops.OPERATIONS if op.name == "show")
    assert "assessment" in show.fields
    views = order_ops.view_operations()
    assert views["show"]["nested"]["assessment"] == [
        "result",
        "reasoning",
        "approach",
        "limit",
        "rounds",
        "at",
    ]
    for name in ("events", "wait"):
        operation = next(
            op for op in order_ops.OPERATIONS if op.name == name
        )
        assert operation.nested[0][1][-1] == "opinion"
    assert "wait" not in views
