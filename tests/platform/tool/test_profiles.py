"""`profile list|set|delete`, `task profile` and `review verdicts` (KOR-51, ADR-090; E-04: what the web offers) against
the hub double with the profile routes of the hub."""

from __future__ import annotations

from typing import Any

import pytest

from agent_platform import profiles
from agent_runner.crypto.ids import uuid7

from .conftest import Tool
from .hubfake import HubFake, User
from .seeding import default_wg

PROFILE = {"name": "review", "operations": ["github.list_reviews", "review.verdict"], "reads": "all"}


def _routes(hub: HubFake) -> list[tuple[str, str, Any]]:
    seen: list[tuple[str, str, Any]] = []

    def record(status: int, answer: Any) -> Any:
        def handler(request: Any, body: Any, **params: str) -> tuple[int, Any]:
            seen.append((request.method, request.path, body))
            return status, answer

        return handler

    hub.add_route(
        "GET", r"/v1/workgroups/[0-9a-f-]+/agent-profiles", record(200, {"profiles": [PROFILE], "classes": {}})
    )
    hub.add_route("PUT", r"/v1/(workgroups|orgs)/[0-9a-f-]+/agent-profiles/[a-z0-9_-]+", record(200, {}))
    hub.add_route("DELETE", r"/v1/(workgroups|orgs)/[0-9a-f-]+/agent-profiles/[a-z0-9_-]+", record(200, {}))
    hub.add_route("PUT", r"/v1/tasks/[0-9a-f-]+/agent-profile", record(200, {"profile": PROFILE}))
    return seen


def test_profiles(tool: Tool, hub: HubFake, account: User) -> None:
    seen = _routes(hub)
    wg, org = default_wg(hub, account), uuid7()
    assert tool.run("profile", "list", "--wg", wg)[1]["profiles"] == [PROFILE]
    code, saved = tool.run(
        "profile",
        "set",
        "--wg",
        wg,
        "--name",
        "triage",
        "--operation",
        "github.get_issue",
        "--operation",
        "github.get_issue",
        "--reads",
        "task",
    )
    assert code == 0, saved
    assert saved == {"name": "triage", "operations": ["github.get_issue"], "reads": "task", "scope": "wg"}
    assert seen[-1] == (
        "PUT",
        f"/v1/workgroups/{wg}/agent-profiles/triage",
        {"operations": ["github.get_issue"], "reads": "task"},
    )
    assert tool.run("profile", "set", "--org", org, "--name", "auditor")[1]["scope"] == "org"
    assert seen[-1][1] == f"/v1/orgs/{org}/agent-profiles/auditor"
    assert tool.run("profile", "delete", "--org", org, "--name", "auditor")[1] == {"name": "auditor", "deleted": True}
    assert tool.run("profile", "delete", "--wg", wg, "--name", "triage")[1]["deleted"] is True
    assert tool.run("profile", "set", "--name", "x")[1]["reason"] == "wg_or_org"
    assert tool.run("profile", "set", "--wg", wg, "--org", org, "--name", "x")[1]["reason"] == "wg_or_org"
    assert tool.run("profile", "set", "--wg", wg, "--name", "Bad Name")[1]["reason"] == "profile_invalid"


def test_task_profile(tool: Tool, hub: HubFake, account: User) -> None:
    seen = _routes(hub)
    task = uuid7()
    assert tool.run("task", "profile", "--task", task, "--profile", "review")[1] == {
        "task_id": task,
        "profile": PROFILE,
    }
    assert seen[-1][2] == {"profile": "review"}
    tool.run("task", "profile", "--task", task, "--clear")
    assert seen[-1][2] == {"profile": None}
    assert tool.run("task", "profile", "--task", task)[1]["reason"] == "profile_or_clear"
    assert tool.run("task", "profile", "--task", task, "--profile", "x", "--clear")[1]["reason"] == "profile_or_clear"
    assert account.id


def test_review_verdicts(tool: Tool, monkeypatch: pytest.MonkeyPatch) -> None:
    task = uuid7()
    verdict = {"verdict_id": "v", "outcome": "passed"}
    rows = [
        {
            "operation_id": "a",
            "type": "review.verdict",
            "session_id": "s",
            "state": "succeeded",
            "events": [
                {"kind": "operation.params", "body": {}},
                {"kind": "operation.result", "body": verdict},
            ],
        },
        {"operation_id": "b", "type": "security.report", "session_id": "s", "state": "failed", "events": []},
        {"operation_id": "c", "type": "github.push", "session_id": "s", "state": "succeeded", "events": []},
    ]

    def listing(_account: Any, wanted: str, _before: Any, _operation: Any) -> dict[str, Any]:
        assert wanted == task
        return {"task_id": task, "operations": rows, "next_before": None, "unverified": 2}

    monkeypatch.setattr(profiles, "task_operations", listing)
    code, found = tool.run("review", "verdicts", "--task", task)
    assert code == 0, found
    assert [(item["operation_id"], item["result"]) for item in found["items"]] == [("a", verdict), ("b", None)]
    assert found["unverified"] == 2
