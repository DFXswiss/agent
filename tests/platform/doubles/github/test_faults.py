"""U-DBL-github: fault injection and expectations (07-testing.md §9; rate-limit rules of 05-runner.md §12.1)."""

from __future__ import annotations

import http.client
import time
from typing import Any

import pytest

from agent_doubles.github.faults import FaultError, Plan, parse_expectation, parse_fault
from agent_doubles.github.server import ROUTE_KEYS

REFS = "POST /repos/{owner}/{repo}/git/refs"
COMMENTS = "POST /repos/{owner}/{repo}/issues/{number}/comments"
USER = "GET /user"
TREE_GET = "GET /repos/{owner}/{repo}/git/trees/{tree}"
TREE_POST = "POST /repos/{owner}/{repo}/git/trees"
ISSUES = "GET /repos/{owner}/{repo}/issues"


def faults(gh: Any, *items: dict[str, Any]) -> Any:
    return gh.control("POST", "faults", {"faults": list(items)})


def test_server_errors_then_success(github: Any) -> None:
    assert faults(github, {"route": USER, "kind": "error", "status": 503, "times": 2}).status == 200
    assert github.state()["done"] is False
    assert github.state()["pending"] == ["fault error on GET /user fired 0 of 2 times"]
    first = github.api("GET", "/user")
    assert (first.status, first.json()["message"]) == (503, "Server Error")
    assert github.api("GET", "/user").status == 503
    assert github.api("GET", "/user").status == 200
    state = github.state()
    assert (state["done"], state["ok"]) == (True, True)
    journal = github.client.journal()
    assert [(e["status"], e.get("fault")) for e in journal] == [(503, "error"), (503, "error"), (200, None)]
    assert journal[0]["note"] == "fault error"
    assert github.control("GET", "faults").json()["faults"] == [
        {"route": USER, "repo": None, "kind": "error", "status": 503, "fired": 2, "times": 2}
    ]


def test_rate_limits(github: Any) -> None:
    faults(
        github,
        {"route": USER, "kind": "retry_after", "retry_after": 3},
        {"route": USER, "kind": "secondary_rate_limit", "retry_after": 60},
        {"route": USER, "kind": "secondary_rate_limit"},
        {"route": USER, "kind": "primary_rate_limit", "reset_in": 30},
        {"route": USER, "kind": "primary_rate_limit", "status": 429},
        {"route": USER, "kind": "exhaust", "reset_in": 20},
    )
    too_many = github.api("GET", "/user")
    assert (too_many.status, too_many.headers["retry-after"]) == (429, "3")
    secondary = github.api("GET", "/user")
    assert (secondary.status, secondary.headers["retry-after"]) == (403, "60")
    assert "secondary rate limit" in secondary.json()["message"]
    without = github.api("GET", "/user")
    assert without.status == 403
    assert "retry-after" not in without.headers
    primary = github.api("GET", "/user")
    assert primary.status == 403
    assert primary.headers["x-ratelimit-remaining"] == "0"
    assert primary.headers["x-ratelimit-reset"] == str(int(github.clock.now) + 30)
    assert primary.json()["message"].startswith("API rate limit exceeded")
    assert github.api("GET", "/user").status == 429
    exhausted = github.api("GET", "/user")
    assert exhausted.status == 200
    assert exhausted.json()["login"] == "octo-user"
    assert exhausted.headers["x-ratelimit-remaining"] == "0"
    assert exhausted.headers["x-ratelimit-reset"] == str(int(github.clock.now) + 20)
    assert github.api("GET", "/user").headers["x-ratelimit-remaining"] != "0"
    assert github.state()["ok"] is True


def test_slow_and_disconnect(github: Any) -> None:
    faults(github, {"route": USER, "kind": "slow", "delay_ms": 200}, {"route": USER, "kind": "disconnect"})
    started = time.monotonic()
    assert github.api("GET", "/user").status == 200
    assert time.monotonic() - started >= 0.2
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError)):
        github.api("GET", "/user")
    assert github.api("GET", "/user").status == 200
    assert [e.get("fault") for e in github.client.journal()] == ["slow", "disconnect", None]
    assert github.state()["ok"] is True


def test_skip_repo_filter_and_any_route(github: Any) -> None:
    faults(
        github,
        {"route": "*", "repo": "Octo-Org/Second", "kind": "error"},
        {"route": "GET /repos/{owner}/{repo}", "kind": "error", "skip": 1, "status": 500},
    )
    assert github.api("GET", "/repos/octo-org/hello").status == 200
    assert github.api("GET", "/repos/octo-org/hello").status == 500
    assert github.api("GET", "/repos/octo-org/hello").status == 200
    assert github.api("GET", "/repos/octo-org/second").status == 502
    assert github.api("GET", "/repos/octo-org/second").status == 200
    assert github.state()["ok"] is True


def test_faults_hit_the_device_flow_and_the_seed_arms_them(start_github: Any, seed: Any) -> None:
    data = seed()
    data["faults"] = [{"route": "POST /login/oauth/access_token", "kind": "error", "status": 500}]
    gh = start_github(data)
    assert gh.client.request("POST", "/login/oauth/access_token", {}).status == 500
    assert gh.state()["ok"] is True


def test_expectations(github: Any) -> None:
    github.control(
        "POST",
        "expect",
        {"expectations": [{"route": COMMENTS, "repo": "octo-org/hello", "count": 1}, {"route": USER, "count": 0}]},
    )
    assert github.state()["pending"] == [f"expected {COMMENTS} 1 times, seen 0"]
    github.api("POST", "/repos/octo-org/hello/pulls", {"title": "t", "head": "feature", "base": "main"})
    assert github.api("POST", "/repos/octo-org/hello/issues/1/comments", {"body": "b"}).status == 201
    assert github.state()["ok"] is True
    github.api("POST", "/repos/octo-org/hello/issues/1/comments", {"body": "b"})
    github.api("GET", "/user")
    state = github.state()
    assert state["failures"] == [
        f"{COMMENTS} called 2 times, expected 1 for octo-org/hello",
        f"{USER} called 1 times, expected 0",
    ]
    assert github.control("GET", "faults").json()["expectations"] == [
        {"route": COMMENTS, "repo": "octo-org/hello", "count": 1, "seen": 2},
        {"route": USER, "repo": None, "count": 0, "seen": 1},
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("x", "a fault must be an object"),
        ({"route": USER, "kind": "error", "extra": 1}, "unknown fault keys"),
        ({"route": "GET /nowhere", "kind": "error"}, "unknown route"),
        ({"route": USER, "kind": "error", "repo": 5}, "repo must be a string"),
        ({"route": USER, "kind": "boom"}, "kind must be one of"),
        ({"route": USER, "kind": "error", "status": 404}, "an error fault needs a status"),
        ({"route": USER, "kind": "primary_rate_limit", "status": 500}, "a primary rate limit answers 403 or 429"),
        ({"route": USER, "kind": "retry_after", "status": 503}, "a retry_after fault answers 429"),
        ({"route": USER, "kind": "secondary_rate_limit", "status": 429}, "a secondary_rate_limit fault answers 403"),
        ({"route": USER, "kind": "slow", "delay_ms": -1}, "delay_ms must be an integer >= 0"),
        ({"route": USER, "kind": "error", "times": 0}, "times must be an integer >= 1"),
        ({"route": USER, "kind": "error", "skip": True}, "skip must be an integer >= 0"),
        ({"route": USER, "kind": "tree_mismatch"}, "applies to POST /repos/{owner}/{repo}/git/trees only"),
        ({"route": "*", "kind": "truncated"}, "applies to GET /repos/{owner}/{repo}/git/trees/{tree} only"),
        ({"route": TREE_GET, "kind": "truncated", "limit": -1}, "limit must be an integer >= 0"),
    ],
)
def test_fault_refusals(raw: Any, message: str) -> None:
    with pytest.raises(FaultError, match=message):
        parse_fault(raw, ROUTE_KEYS)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "an expectation must be an object"),
        ({"route": USER, "times": 1}, "unknown expectation keys"),
        ({"route": "x"}, "unknown route"),
        ({"route": USER, "count": -1}, "count must be an integer >= 0"),
    ],
)
def test_expectation_refusals(raw: Any, message: str) -> None:
    with pytest.raises(FaultError, match=message):
        parse_expectation(raw, ROUTE_KEYS)


def test_control_route_refusals(github: Any) -> None:
    for path, body in (
        ("faults", []),
        ("faults", {"faults": {}}),
        ("faults", {"faults": [{"route": "x", "kind": "error"}]}),
        ("expect", {"expectations": "x"}),
        ("seed", {"faults": [{"route": USER, "kind": "nope"}]}),
    ):
        response = github.control("POST", path, body)
        assert response.status == 400, path
    assert github.control("GET", "faults").json() == {"faults": [], "expectations": []}


def test_plan_clear() -> None:
    plan = Plan()
    plan.add_faults([parse_fault({"route": USER, "kind": "error"}, ROUTE_KEYS)])
    plan.add_expectations([parse_expectation({"route": USER}, ROUTE_KEYS)])
    assert len(plan.pending()) == 2
    plan.clear()
    assert plan.pending() == []
    assert plan.take(USER, None) is None


def test_faults_keep_the_header_checks_and_rate_headers(github: Any) -> None:
    faults(github, {"route": USER, "kind": "error"}, {"route": USER, "kind": "retry_after"})
    unchecked = github.client.request("GET", "/user", headers={"authorization": "Bearer double-user-token"})
    assert unchecked.status == 502
    assert unchecked.headers["x-ratelimit-remaining"] == "4999"
    assert unchecked.headers["x-github-api-version-selected"] == "2022-11-28"
    assert github.state()["failures"] == [
        "GET /user: missing X-GitHub-Api-Version",
        "GET /user: Accept is not application/vnd.github+json",
    ]
    anonymous = github.api("GET", "/user", token=None)
    assert anonymous.status == 401
    assert github.control("GET", "faults").json()["faults"][1]["fired"] == 0
    assert github.api("GET", "/user").status == 429


def test_tree_mismatch_and_truncated_listing(github: Any) -> None:
    faults(github, {"route": TREE_POST, "kind": "tree_mismatch"}, {"route": TREE_GET, "kind": "truncated", "limit": 2})
    body = {"tree": [{"path": "a", "mode": "100644", "type": "blob", "content": "a"}]}
    mismatched = github.api("POST", "/repos/octo-org/hello/git/trees", body).json()
    honest = github.api("POST", "/repos/octo-org/hello/git/trees", body).json()
    assert mismatched["sha"] != honest["sha"]
    assert [e["path"] for e in mismatched["tree"]] == [".double-tree-mismatch", "a"]
    tree = github.get("/repos/octo-org/hello/commits/main")["commit"]["tree"]["sha"]
    cut = github.get(f"/repos/octo-org/hello/git/trees/{tree}?recursive=1")
    assert (len(cut["tree"]), cut["truncated"]) == (2, True)
    whole = github.get(f"/repos/octo-org/hello/git/trees/{tree}?recursive=1")
    assert (len(whole["tree"]), whole["truncated"]) == (3, False)
    assert github.state()["ok"] is True


def test_rate_limits_on_the_issue_list(github: Any) -> None:
    faults(
        github,
        {"route": ISSUES, "kind": "primary_rate_limit", "reset_in": 30},
        {"route": ISSUES, "kind": "secondary_rate_limit", "retry_after": 60},
        {"route": "POST /repos/{owner}/{repo}/issues", "kind": "error", "status": 502},
    )
    path = "/repos/octo-org/hello/issues"
    primary = github.api("GET", path)
    assert (primary.status, primary.headers["x-ratelimit-remaining"]) == (403, "0")
    assert primary.headers["x-ratelimit-reset"] == str(int(github.clock.now) + 30)
    assert primary.json()["message"].startswith("API rate limit exceeded")
    secondary = github.api("GET", path)
    assert (secondary.status, secondary.headers["retry-after"]) == (403, "60")
    assert "secondary rate limit" in secondary.json()["message"]
    assert github.api("GET", path).json() == []
    assert github.api("POST", path, {"title": "t"}).status == 502
    assert github.api("POST", path, {"title": "t"}).status == 201
    assert [e.get("fault") for e in github.client.journal()] == [
        "primary_rate_limit",
        "secondary_rate_limit",
        None,
        "error",
        None,
    ]
    state = github.state()
    assert (state["ok"], state["done"]) == (True, True)
