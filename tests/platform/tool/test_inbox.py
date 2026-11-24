"""`notifications list|read`, `quotas`, `monitoring` and `audit` against the hub double, with the web client's checks
of the answers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_platform import inbox
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.account_views import add_notification, views
from .hubfake import HubFake, User, now_ms_text
from .seeding import private_org, reader


def test_notifications(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    first = add_notification(hub, account.id, org=org)
    second = add_notification(hub, account.id, org=org)
    agent = add_notification(hub, account.id, kind="agent.account_change")
    add_notification(hub, uuid7(), org=org)
    code, listed = tool.run("notifications", "list")
    assert code == 0, listed
    assert [row["notification_id"] for row in listed["notifications"]] == [agent, second, first]
    assert (listed["unread"], listed["next_before"]) == (2, None)
    assert tool.run("notifications", "read", "--id", first)[1]["read"] == 1
    unread = tool.run("notifications", "list", "--unread")[1]
    assert [row["notification_id"] for row in unread["notifications"]] == [agent, second]
    assert tool.run("notifications", "read", "--all")[1]["read"] == "all"
    assert [row["notification_id"] for row in tool.run("notifications", "list", "--unread")[1]["notifications"]] == [
        agent
    ]
    for argv in ((), ("--all", "--id", first), ("--id", "x")):
        assert tool.run("notifications", "read", *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("notifications", "read", "--id", uuid7())[1]["read"] == 1
    too_many = [arg for _ in range(inbox.READ_MAX + 1) for arg in ("--id", uuid7())]
    assert tool.run("notifications", "read", *too_many)[1]["error"] == "invalid_argument"
    # paging and malformed rows
    hub.hooks[("GET", "/v1/notifications")] = lambda body, request: (
        200,
        {"notifications": [{"notification_id": 1}, "x"], "next_before": "n"},
    )
    paged = tool.run("notifications", "list", "--before", "c")[1]
    assert (paged["notifications"], paged["next_before"]) == ([], "n")
    assert [r.query for r in hub.requests if r.path == "/v1/notifications"][-1] == {
        "account": "1",
        "limit": "50",
        "before": "c",
    }
    hub.hooks[("GET", "/v1/notifications")] = lambda body, request: (200, {"notifications": None})
    assert tool.run("notifications", "list")[1]["reason"] == "response_invalid"


def test_quotas(tool: Tool, hub: HubFake, account: User) -> None:
    assert tool.run("quotas")[1] == {"ok": True, "available": False, "quotas": []}
    older, newer = "2026-11-24T08:00:00.000Z", "2026-11-24T09:00:00.000Z"
    base = {"severity": "ok", "device_id": uuid7(), "resets_at": None}
    views(hub).quotas[account.id] = [
        {**base, "window": "weekly:opus", "used_percent": 10, "fetched_at": older},
        {**base, "window": "seven_day", "used_percent": 20.5, "fetched_at": older},
        {**base, "window": "five_hour", "used_percent": 30, "fetched_at": older, "resets_at": newer},
        {**base, "window": "five_hour", "used_percent": 40, "fetched_at": newer, "severity": None},
        {**base, "window": "five_hour", "used_percent": 1, "fetched_at": older},
        {**base, "window": "weekly:haiku", "used_percent": 5, "fetched_at": older},
        {**base, "window": "monthly", "used_percent": 1, "fetched_at": older},
        {**base, "window": "seven_day", "used_percent": -1, "fetched_at": newer},
        {**base, "window": "seven_day", "used_percent": True, "fetched_at": newer},
        {**base, "window": "seven_day", "used_percent": 1, "fetched_at": "never"},
        {**base, "window": "seven_day", "used_percent": 1, "fetched_at": newer, "resets_at": "soon"},
        {**base, "window": "seven_day", "used_percent": 1, "fetched_at": 5},
    ]
    code, shown = tool.run("quotas")
    assert code == 0, shown
    assert [(bar["window"], bar["used_percent"]) for bar in shown["quotas"]] == [
        ("five_hour", 40),
        ("seven_day", 20.5),
        ("weekly:haiku", 5),
        ("weekly:opus", 10),
    ]
    assert (shown["quotas"][0]["severity"], shown["quotas"][2]["model"], shown["quotas"][0]["model"]) == (
        None,
        "haiku",
        None,
    )


def test_monitoring(tool: Tool, hub: HubFake, account: User, monkeypatch: pytest.MonkeyPatch) -> None:
    org = private_org(hub, account)
    views(hub).overview[org] = {"devices": [{"device_id": "d"}, "x"], "vms": [], "sessions": []}
    code, shown = tool.run("monitoring", "--org", org)
    assert code == 0, shown
    assert shown["devices"] == [{"device_id": "d"}]
    assert tool.run("monitoring", "--org", uuid7())[1]["hub_error"] == "not_found"
    views(hub).points = [
        ["2026-11-24T08:00:00.000Z", 0.5],
        ["2026-11-24T08:00:15.000Z", None],
        ["x"],
        [1, 2],
        ["t", "v"],
    ]
    assert inbox.now_ms() > 1_700_000_000_000
    monkeypatch.setattr(inbox, "now_ms", lambda: 1_000_000_000)
    session = uuid7()
    series = tool.run(
        "monitoring", "--org", org, "--entity-kind", "session", "--entity-id", session, "--metric", "cpu_ratio"
    )[1]
    assert series["points"] == [["2026-11-24T08:00:00.000Z", 0.5], ["2026-11-24T08:00:15.000Z", None]]
    asked = views(hub).series_queries[-1]
    assert (asked["from"], asked["to"], asked["step"]) == ("1970-01-12T12:46:45.000Z", "1970-01-12T13:46:45.000Z", "15")
    vm = tool.run(
        "monitoring", "--org", org, "--entity-kind", "vm", "--entity-id", "vm-0a1b2c3d", "--metric", "net_rx_bytes"
    )
    assert vm[1]["entity_id"] == "vm-0a1b2c3d"
    for argv in (
        ("--entity-kind", "vm", "--entity-id", "vm-x", "--metric", "cpu_ratio"),
        ("--entity-kind", "device", "--entity-id", "x", "--metric", "cpu_ratio"),
        ("--entity-kind", "device"),
    ):
        assert tool.run("monitoring", "--org", org, *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("monitoring", "--org", "x")[1]["error"] == "invalid_argument"
    hub.hooks[("GET", "/v1/monitoring/series")] = lambda body, request: (200, {"points": None})
    ninety = ("--entity-kind", "device", "--entity-id", uuid7(), "--metric", "cpu_ratio", "--range", "90d")
    assert tool.run("monitoring", "--org", org, *ninety)[1]["reason"] == "response_invalid"


def audit_row(org: str | None, actor: str, **extra: Any) -> dict[str, Any]:
    row = {
        "seq": 1,
        "at": now_ms_text(),
        "actor_id": actor,
        "actor_kind": "user",
        "action": "log.grant",
        "result": "ok",
        "org_id": org,
        "wg_id": None,
        "task_id": None,
        "session_id": None,
        "device_id": None,
        "target_kind": None,
        "target_id": None,
        "detail": {"n": 1},
        "event_id": None,
    }
    return {**row, **extra}


def test_audit(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    other, bob, org, wg = reader(tool, hub, tmp_path)
    task = uuid7()
    rows = views(hub).audit
    rows += [
        audit_row(org, account.id),
        audit_row(org, account.id, action="operation.succeeded", task_id=task, wg_id=wg),
        audit_row(org, bob.id, action="task.close"),
        audit_row(None, account.id, action="auth.login"),
        audit_row(None, account.id, action="agent.claim", agent_id=next(iter(hub.agents))),
        audit_row(org, account.id, detail={"bad": [1]}),
    ]
    code, mine = tool.run("audit")
    assert code == 0, mine
    assert [row["action"] for row in mine["rows"]] == ["agent.claim", "auth.login"]
    shown = tool.run("audit", "--org", org)[1]
    assert [row["action"] for row in shown["rows"]] == ["task.close", "operation.succeeded", "log.grant"]
    filtered = tool.run("audit", "--org", org, "--action", "operation", "--task", task, "--wg", wg)[1]
    assert [row["action"] for row in filtered["rows"]] == ["operation.succeeded"]
    by = tool.run("audit", "--org", org, "--actor", bob.id, "--before", "c")[1]
    assert [row["action"] for row in by["rows"]] == ["task.close"]
    # a member sees own rows and rows of readable workgroups only
    assert [row["action"] for row in other.run("audit", "--org", org)[1]["rows"]] == [
        "task.close",
        "operation.succeeded",
    ]
    verified = tool.run("audit", "--org", org, "--verify")[1]
    assert (verified["ok"], verified["head"]["seq"], verified["checkpoint"]) == (True, 6, None)
    assert other.run("audit", "--org", org, "--verify")[1]["hub_error"] == "not_found"
    for argv in (("--verify",), ("--action", "x"), ("--org", org, "--action", "Bad"), ("--org", org, "--wg", "x")):
        assert tool.run("audit", *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("audit", "--org", org, "--action", "a" * 97)[1]["error"] == "invalid_argument"
    hub.hooks[("GET", f"/v1/orgs/{org}/audit/verify")] = lambda body, request: (200, {"ok": "yes", "head": {}})
    assert tool.run("audit", "--org", org, "--verify")[1]["reason"] == "response_invalid"
    hub.hooks[("GET", "/v1/me/audit")] = lambda body, request: (200, {"rows": [{"seq": 1}], "next_before": "n"})
    assert tool.run("audit")[1] == {"ok": True, "org_id": None, "rows": [], "next_before": "n"}
