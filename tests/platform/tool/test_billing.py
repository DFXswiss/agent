"""`billing --org` and `task charges --task` against the hub double, with the web client's checks of the answers
(`web/src/core/billing/billing.ts`)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from agent_platform import billing as billing_module
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.billing import PRICES, billing, consumption
from .fakes.sessions import add_session
from .hubfake import HubFake, User
from .seeding import default_wg, new_task, private_org, reader


def test_billing_of_an_org(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    wg = default_wg(hub, account)
    # before a price list is in force nothing is charged and nothing is blocked
    code, idle = tool.run("billing", "--org", org)
    assert code == 0, idle
    assert idle == {
        "ok": True,
        "org_id": org,
        "balance_micro_usd": 0,
        "exhausted": False,
        "billing_active": False,
        "price_list_version": None,
        "token_prices": [],
        "hourly": [],
        "credits": [],
        "days": [],
        "workgroups": [],
    }
    stored = billing(hub)
    stored.prices = copy.deepcopy(PRICES)
    stored.balances[org] = 12_340_000
    stored.credits[org] = [
        {
            "at": "2026-11-24T08:00:00.000Z",
            "kind": "grant",
            "amount_micro_usd": 10_000_000,
            "balance_micro_usd": 12_340_000,
            "reason": "welcome",
            "actor": "operator",
        },
        {
            "at": "2026-11-21T08:00:00.000Z",
            "kind": "start",
            "amount_micro_usd": 2_340_000,
            "balance_micro_usd": 2_340_000,
            "reason": None,
            "actor": "hub",
        },
        {"at": 5, "kind": "grant"},
        "x",
    ]
    stored.days[org] = [{"day": "2026-11-24", **consumption(1200, 3400, 900, 60_000)}, {"day": 5}]
    stored.workgroups[org] = [{"wg_id": wg, **consumption(1200, 3400, 900, 60_000)}, {"wg_id": wg, "tokens": "x"}]
    code, shown = tool.run("billing", "--org", org)
    assert code == 0, shown
    assert (shown["balance_micro_usd"], shown["exhausted"], shown["billing_active"]) == (12_340_000, False, True)
    assert shown["price_list_version"] == 1
    # customer prices: the purchase price times the factor of the platform access, per VM hour without `default`
    assert shown["token_prices"] == [
        {"model": "claude-sonnet-5-5", "input_usd_per_million": 3.0, "output_usd_per_million": 15.0}
    ]
    assert shown["hourly"] == [
        {"size": "session", "usd_per_hour": 0.36},
        {"size": "standard", "usd_per_hour": 0.36},
        {"size": "large", "usd_per_hour": 0.72},
        {"size": "xlarge", "usd_per_hour": 1.44},
    ]
    assert [(row["kind"], row["reason"]) for row in shown["credits"]] == [("grant", "welcome"), ("start", None)]
    assert shown["days"] == [
        {"day": "2026-11-24", **consumption(1200, 3400, 900, 60_000), "total_micro_usd": 4600},
    ]
    assert [row["wg_id"] for row in shown["workgroups"]] == [wg]
    # the credit is used up
    stored.balances[org] = -5
    assert tool.run("billing", "--org", org)[1]["exhausted"] is True
    for argv in ((), ("--org", "x")):
        assert tool.run("billing", *argv)[1]["error"] == "invalid_argument", argv
    assert tool.run("billing", "--org", uuid7())[1]["hub_error"] == "not_found"


def test_price_rows_the_web_leaves_out() -> None:
    assert billing_module.price_rows(None) == {"token_prices": [], "hourly": []}
    assert billing_module.price_rows({"tokens": {}, "runtime": []}) == {"token_prices": [], "hourly": []}
    document = {
        "tokens": {
            "factors": {"platform": 2},
            "models": {"a": {"input": 1, "output": 2}, "b": "x", "c": {"input": True, "output": 1}},
        },
        "runtime": {"per_hour": {"standard": 1, "large": "x", "default": 3}},
    }
    assert billing_module.price_rows(document) == {
        "token_prices": [{"model": "a", "input_usd_per_million": 2, "output_usd_per_million": 4}],
        "hourly": [{"size": "standard", "usd_per_hour": 1}],
    }
    # without a factor no token price is shown; models and hours that are not objects count as empty
    assert billing_module.price_rows({"tokens": {"models": {"a": {"input": 1, "output": 2}}}, "runtime": {}}) == {
        "token_prices": [],
        "hourly": [],
    }
    assert billing_module.number(float("nan")) is None


def test_billing_answers_the_web_calls_malformed(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    path = ("GET", f"/v1/orgs/{org}/billing")
    good: dict[str, Any] = {
        "balance_micro_usd": 1,
        "exhausted": False,
        "billing_active": True,
        "price_list": "x",
        "credits": None,
    }
    for broken in ({**good, "balance_micro_usd": "1"}, {**good, "exhausted": None}, {**good, "billing_active": 1}):

        def served(body: Any, request: Any, broken: dict[str, Any] = broken) -> tuple[int, Any]:
            return 200, broken

        hub.hooks[path] = served
        assert tool.run("billing", "--org", org)[1]["reason"] == "response_invalid", broken
    hub.hooks[path] = lambda body, request: (200, good)
    shown = tool.run("billing", "--org", org)[1]
    assert (shown["price_list_version"], shown["credits"], shown["token_prices"]) == (None, [], [])


def test_task_charges(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    task = new_task(tool, default_wg(hub, account))
    code, empty = tool.run("task", "charges", "--task", task)
    assert code == 0, empty
    assert empty == {"ok": True, "task_id": task, "total_micro_usd": 0, "sessions": []}
    first, second = add_session(hub, task), add_session(hub, task)
    billing(hub).charges[task] = [
        {"session_id": first, **consumption(1000, 2000, 500, 30_000)},
        {"session_id": second, **consumption(5, 0, 3, 0)},
        {"session_id": 7, **consumption(1, 1, 1, 1)},
    ]
    shown = tool.run("task", "charges", "--task", task)[1]
    assert shown["total_micro_usd"] == 3007
    assert [(row["session_id"], row["total_micro_usd"]) for row in shown["sessions"]] == [(first, 3000), (second, 5)]
    assert shown["sessions"][0]["runtime_ms"] == 30_000
    # unknown, malformed and foreign IDs alike
    assert tool.run("task", "charges", "--task", "x")[1]["error"] == "invalid_argument"
    assert tool.run("task", "charges", "--task", uuid7())[1]["hub_error"] == "not_found"
    other, _bob, _org, _wg = reader(tool, hub, tmp_path)
    assert other.run("task", "charges", "--task", task)[1]["hub_error"] == "not_found"
    hub.hooks[("GET", f"/v1/tasks/{task}/charges")] = lambda body, request: (200, {"sessions": []})
    assert tool.run("task", "charges", "--task", task)[1]["reason"] == "response_invalid"


def test_consumption_per_person(tool: Tool, hub: HubFake, account: User) -> None:
    """`people` of ADR-083 section 2: shown when the hub sends it, rows of another form left out."""
    org = private_org(hub, account)
    task = new_task(tool, default_wg(hub, account))
    rows: list[Any] = [
        {"user_id": account.id, **consumption(1000, 200, 50, 1000)},
        {"user_id": None, **consumption(5, 0, 3, 0)},
        {"user_id": 7, **consumption(1, 1, 1, 1)},
        {"user_id": account.id, "tokens": "x"},
        "x",
    ]
    expected = [
        {"user_id": account.id, **consumption(1000, 200, 50, 1000), "total_micro_usd": 1200},
        {"user_id": None, **consumption(5, 0, 3, 0), "total_micro_usd": 5},
    ]
    billing(hub).people[org] = rows
    billing(hub).people[task] = rows
    assert tool.run("billing", "--org", org)[1]["people"] == expected
    assert tool.run("task", "charges", "--task", task)[1]["people"] == expected
    billing(hub).people[task] = []
    assert tool.run("task", "charges", "--task", task)[1]["people"] == []
    hub.hooks[("GET", f"/v1/tasks/{task}/charges")] = lambda body, request: (
        200,
        {"sessions": [], "total_micro_usd": 0, "people": "x"},
    )
    assert "people" not in tool.run("task", "charges", "--task", task)[1]
