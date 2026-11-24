"""`quotas --org`, `task model-consent --task` and `settings set --fallback/--fallback-platform` (ADR-081) against the
hub double, with the web client's checks of the answers (`web/src/core/credentials/orgQuotas.ts`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from agent_platform import modelaccess
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .fakes.model_access import model_access
from .hubfake import HubFake, User
from .seeding import default_wg, new_task, private_org, reader

AT = "2026-11-24T13:05:00.000Z"
LATER = "2026-11-24T14:05:00.000Z"
WINDOW = {"window": "five_hour", "used_percent": 100, "resets_at": LATER, "severity": "critical", "fetched_at": AT}
SUBSCRIPTION = {
    "label": "team-a",
    "state": "exhausted",
    "readable": True,
    "used_percent": 100,
    "resets_at": LATER,
    "limited_at": AT,
    "until": LATER,
    "fetched_at": AT,
}
ANSWER: dict[str, Any] = {
    "own": {
        "windows": [WINDOW, {**WINDOW, "window": "monthly"}, "x"],
        "unreadable": None,
        "limited": {"at": AT, "until": LATER, "readable": True},
    },
    "operator": {
        "granted": True,
        "subscriptions": [
            SUBSCRIPTION,
            {
                "label": "team-b",
                "state": "usable",
                "readable": False,
                "used_percent": None,
                "resets_at": None,
                "limited_at": None,
                "until": None,
                "fetched_at": None,
            },
            {**SUBSCRIPTION, "label": ""},
            {**SUBSCRIPTION, "state": 3},
            {**SUBSCRIPTION, "readable": "yes"},
            {**SUBSCRIPTION, "used_percent": "full"},
            {**SUBSCRIPTION, "used_percent": -1},
            {**SUBSCRIPTION, "until": "soon"},
            {**SUBSCRIPTION, "fetched_at": 5},
            "x",
        ],
    },
    "platform": {"available": True},
    "summary": {"usable": 1, "exhausted": 1, "next_release": LATER},
}


def test_org_quotas(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org = private_org(hub, account)
    # nothing readable, no operator subscriptions, no DFX access
    code, empty = tool.run("quotas", "--org", org)
    assert code == 0, empty
    assert empty == {
        "ok": True,
        "org_id": org,
        "own": {"quotas": [], "unreadable_since": None, "limited": None},
        "operator": {"granted": False, "subscriptions": []},
        "platform": {"available": False},
        "summary": {"usable": 0, "exhausted": 0, "next_release": None},
    }
    model_access(hub).quotas[org] = ANSWER
    code, shown = tool.run("quotas", "--org", org)
    assert code == 0, shown
    assert [(bar["window"], bar["used_percent"], bar["resets_at"]) for bar in shown["own"]["quotas"]] == [
        ("five_hour", 100, LATER)
    ]
    assert shown["own"]["limited"] == {"at": AT, "until": LATER, "readable": True}
    assert shown["operator"]["granted"] is True
    assert shown["operator"]["subscriptions"] == [
        SUBSCRIPTION,
        {
            "label": "team-b",
            "state": "usable",
            "readable": False,
            "used_percent": None,
            "resets_at": None,
            "limited_at": None,
            "until": None,
            "fetched_at": None,
        },
    ]
    assert (shown["platform"], shown["summary"]) == (
        {"available": True},
        {"usable": 1, "exhausted": 1, "next_release": LATER},
    )
    # not readable (a long-lived token): said honestly, with the last observed limit; a limit without readable time
    # or with a malformed end is no limit
    unreadable = copy.deepcopy(ANSWER)
    unreadable["own"] = {
        "windows": None,
        "unreadable": {"since": AT},
        "limited": {"at": AT, "until": None, "readable": False},
    }
    unreadable["operator"]["subscriptions"] = None
    model_access(hub).quotas[org] = unreadable
    shown = tool.run("quotas", "--org", org)[1]
    assert shown["own"] == {
        "quotas": [],
        "unreadable_since": AT,
        "limited": {"at": AT, "until": None, "readable": False},
    }
    assert shown["operator"]["subscriptions"] == []
    for limited in (
        "x",
        {"at": AT, "until": None},
        {"at": None, "until": None, "readable": True},
        {"at": "now", "until": None, "readable": True},
        {"at": AT, "until": "soon", "readable": True},
    ):
        unreadable["own"] = {"windows": [], "unreadable": {"since": "yesterday"}, "limited": limited}
        shown = tool.run("quotas", "--org", org)[1]
        assert (shown["own"]["limited"], shown["own"]["unreadable_since"]) == (None, None), limited
    # members only; unknown and malformed IDs
    assert tool.run("quotas", "--org", uuid7())[1]["hub_error"] == "not_found"
    assert tool.run("quotas", "--org", "x")[1]["error"] == "invalid_argument"
    other, _bob, team, _wg = reader(tool, hub, tmp_path)
    assert other.run("quotas", "--org", org)[1]["hub_error"] == "not_found"
    assert other.run("quotas", "--org", team)[1]["summary"]["usable"] == 0
    # without --org the user's own quotas as before
    assert tool.run("quotas")[1] == {"ok": True, "available": False, "quotas": [], "unreadable_since": None}


def test_org_quotas_answers_the_web_calls_malformed(tool: Tool, hub: HubFake, account: User) -> None:
    org = private_org(hub, account)
    path = ("GET", f"/v1/orgs/{org}/quotas")
    good = copy.deepcopy(ANSWER)
    broken_answers: list[dict[str, Any]] = [
        {**good, "own": None},
        {**good, "operator": "x"},
        {**good, "platform": None},
        {**good, "summary": None},
        {**good, "operator": {"granted": 1, "subscriptions": []}},
        {**good, "platform": {"available": None}},
        {**good, "summary": {"usable": 1, "exhausted": True, "next_release": None}},
        {**good, "summary": {"usable": "1", "exhausted": 0, "next_release": None}},
        {**good, "summary": {"usable": 1, "exhausted": 0, "next_release": "soon"}},
    ]
    for broken in broken_answers:

        def served(body: Any, request: Any, broken: dict[str, Any] = broken) -> tuple[int, Any]:
            return 200, broken

        hub.hooks[path] = served
        assert tool.run("quotas", "--org", org)[1]["reason"] == "response_invalid", broken


def test_model_consent(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    task = new_task(tool, default_wg(hub, account))
    code, shown = tool.run("task", "model-consent", "--task", task)
    assert code == 0, shown
    assert shown == {"ok": True, "task_id": task, "model_consent": True}
    assert model_access(hub).consents == [task]
    # writers only; unknown and malformed IDs
    assert tool.run("task", "model-consent", "--task", "x")[1]["error"] == "invalid_argument"
    assert tool.run("task", "model-consent", "--task", uuid7())[1]["hub_error"] == "not_found"
    other, _bob, _org, _wg = reader(tool, hub, tmp_path)
    assert other.run("task", "model-consent", "--task", task)[1]["hub_error"] == "not_found"
    assert model_access(hub).consents == [task]
    hub.hooks[("POST", f"/v1/tasks/{task}/model-consent")] = lambda body, request: (200, {"task_id": task})
    assert tool.run("task", "model-consent", "--task", task)[1]["reason"] == "response_invalid"


def test_settings_fallback(tool: Tool, hub: HubFake, account: User) -> None:
    # a hub of an older release (no `fallback` in the settings) gets the body it knows
    document = tool.run("settings", "set", "--locale", "de")[1]
    assert document["settings"] == {"model_access": "auto", "model_id": None, "locale": "de"}
    # an option asks for it: the other one keeps its default
    document = tool.run("settings", "set", "--fallback", "on")[1]
    assert document["settings"]["fallback"] == {"own_to_dfx": True, "operator_to_platform": True}
    document = tool.run("settings", "set", "--fallback-platform", "off")[1]
    assert document["settings"]["fallback"] == {"own_to_dfx": True, "operator_to_platform": False}
    # the stored settings are sent back unchanged with another change
    document = tool.run("settings", "set", "--model-access", "subscription")[1]
    assert document["settings"] == {
        "model_access": "subscription",
        "model_id": None,
        "locale": "de",
        "fallback": {"own_to_dfx": True, "operator_to_platform": False},
    }
    sent = [json.loads(r.raw) for r in hub.requests if (r.method, r.path) == ("PUT", "/v1/me/settings")]
    assert sent[-1]["fallback"] == {"own_to_dfx": True, "operator_to_platform": False}
    assert "fallback" not in sent[0]
    document = tool.run("settings", "set", "--fallback", "off", "--fallback-platform", "on")[1]
    assert document["settings"]["fallback"] == {"own_to_dfx": False, "operator_to_platform": True}
    assert tool.run("settings", "set", "--fallback", "maybe")[1]["error"] == "invalid_argument"


def test_fallback_body() -> None:
    assert modelaccess.fallback_body(None, None, None) is None
    assert modelaccess.fallback_body({"fallback": None}, None, None) is None
    assert modelaccess.fallback_body({"fallback": {"own_to_dfx": "yes"}}, None, None) == {
        "own_to_dfx": False,
        "operator_to_platform": True,
    }
    assert modelaccess.fallback_body("broken", "on", "off") == {"own_to_dfx": True, "operator_to_platform": False}
