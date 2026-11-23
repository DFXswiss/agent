"""U-DBL-github: the double answers with the shapes of real GitHub answers.

The fixtures under ``fixtures/`` are real GitHub answers (``source: recorded``, recorded from a public example
repository) or the examples of the REST reference (device flow, installations, created blob), with every value
replaced by a neutral one of the same JSON type, so they hold no personal data. The double must send exactly the same
keys, recursively, with the same types (``null`` on either side matches anything; lists compare their first items).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_doubles.github.deviceflow import DEFAULT_CLIENT_ID, GRANT_TYPE

REPO = "/repos/octo-org/hello"


def shape_problems(expected: Any, actual: Any, where: str = "$") -> list[str]:
    """Differences in keys and JSON types between a fixture and an answer."""
    problems: list[str] = []
    if expected is None or actual is None:
        pass
    elif isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: object expected"]
        problems += [f"{where}.{key}: missing" for key in sorted(set(expected) - set(actual))]
        problems += [f"{where}.{key}: not sent by GitHub" for key in sorted(set(actual) - set(expected))]
        for key in sorted(set(expected) & set(actual)):
            problems += shape_problems(expected[key], actual[key], f"{where}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{where}: list expected"]
        if expected and actual:
            problems += shape_problems(expected[0], actual[0], f"{where}[0]")
    elif type(actual) is not type(expected) and not (type(expected) is float and type(actual) is int):
        problems.append(f"{where}: {type(expected).__name__} expected, got {type(actual).__name__}")
    return problems


def test_shape_problems_reports_every_kind_of_difference() -> None:
    assert shape_problems({"a": 1, "b": [{"c": "x"}], "d": None}, {"a": 2, "b": [{"c": "y"}], "d": 5}) == []
    assert shape_problems({"a": 1.0}, {"a": 1}) == []
    assert shape_problems({"a": 1, "b": 2}, {"b": "x", "c": 3}) == [
        "$.a: missing",
        "$.c: not sent by GitHub",
        "$.b: int expected, got str",
    ]
    assert shape_problems({"a": {}}, {"a": []}) == ["$.a: object expected"]
    assert shape_problems({"a": []}, {"a": {}}) == ["$.a: list expected"]
    assert shape_problems({"a": True}, {"a": 1}) == ["$.a: bool expected, got int"]
    assert shape_problems([], [1]) == []


def answers(gh: Any) -> dict[str, Any]:
    """One answer of the double per fixture, produced through the routes the fixture names."""
    out: dict[str, Any] = {}
    gh.double.seed(
        {
            "installations": [
                {"id": 77, "account": "octo-org", "users": ["octo-user"], "repositories": ["octo-org/hello"]}
            ]
        }
    )
    main = gh.get(f"{REPO}/commits/main")
    tree = main["commit"]["tree"]["sha"]
    blob = gh.api("POST", f"{REPO}/git/blobs", {"content": base64.b64encode(b"new\n").decode(), "encoding": "base64"})
    out["blob-created"] = blob.json()
    out["blob"] = gh.get(f"{REPO}/git/blobs/{blob.json()['sha']}")
    out["tree"] = gh.get(f"{REPO}/git/trees/{tree}?recursive=1")
    changed = gh.api(
        "POST",
        f"{REPO}/git/commits",
        {
            "message": "Drop the sources.",
            "tree": gh.api(
                "POST",
                f"{REPO}/git/trees",
                {"base_tree": tree, "tree": [{"path": "src", "mode": "040000", "type": "tree", "sha": None}]},
            ).json()["sha"],
            "parents": [main["sha"]],
        },
    ).json()
    out["git-commit"] = gh.get(f"{REPO}/git/commits/{changed['sha']}")
    out["commit"] = gh.get(f"{REPO}/commits/{changed['sha']}")
    out["ref"] = gh.get(f"{REPO}/git/ref/heads/main")
    out["compare"] = gh.get(f"{REPO}/compare/main...{changed['sha']}")
    gh.api("POST", f"{REPO}/pulls", {"title": "Feature", "head": "feature", "base": "main"})
    gh.api("POST", f"{REPO}/issues/1/comments", {"body": "Looks good."})
    out["pull"] = gh.get(f"{REPO}/pulls/1")
    out["pulls"] = gh.get(f"{REPO}/pulls")
    out["pull-files"] = gh.get(f"{REPO}/pulls/1/files")
    out["issue-comments"] = gh.get(f"{REPO}/issues/1/comments")
    out["error-422-validation"] = gh.api(
        "POST", f"{REPO}/pulls", {"title": "t", "head": "feature", "base": "main"}
    ).json()
    out["user"] = gh.get("/user")
    out["repository"] = gh.get(REPO)
    out["installations"] = gh.get("/user/installations")
    out["installation-repositories"] = gh.get("/user/installations/31/repositories")
    out["error-404"] = gh.api("GET", "/repos/octo-org/none").json()
    out["error-422"] = gh.api("GET", f"{REPO}/commits/none").json()
    json_headers = {"accept": "application/json"}
    code = gh.client.request("POST", "/login/device/code", {"client_id": DEFAULT_CLIENT_ID}, json_headers).json()
    out["device-code"] = code
    poll = {"client_id": DEFAULT_CLIENT_ID, "device_code": code["device_code"], "grant_type": GRANT_TYPE}
    out["access-token-pending"] = gh.client.request("POST", "/login/oauth/access_token", poll, json_headers).json()
    out["access-token-slow-down"] = gh.client.request("POST", "/login/oauth/access_token", poll, json_headers).json()
    gh.control("POST", "device", {"user_code": code["user_code"], "action": "authorize", "login": "octo-user"})
    gh.clock.now += 60
    out["access-token"] = gh.client.request("POST", "/login/oauth/access_token", poll, json_headers).json()
    return out


FIXTURE_NAMES = (
    "access-token",
    "access-token-pending",
    "access-token-slow-down",
    "blob",
    "blob-created",
    "commit",
    "compare",
    "device-code",
    "error-404",
    "error-422",
    "error-422-validation",
    "git-commit",
    "installation-repositories",
    "installations",
    "issue-comments",
    "pull",
    "pull-files",
    "pulls",
    "ref",
    "repository",
    "tree",
    "user",
)


def test_answers_have_the_shapes_of_github(github: Any, fixture_json: Callable[[str], Any]) -> None:
    produced = answers(github)
    assert sorted(produced) == sorted(FIXTURE_NAMES)
    problems = {name: shape_problems(fixture_json(name)["body"], produced[name]) for name in FIXTURE_NAMES}
    assert problems == {key: [] for key in FIXTURE_NAMES}
    sources = {fixture_json(name)["source"] for name in FIXTURE_NAMES}
    assert sources == {"recorded", "docs.github.com REST reference example (values neutralised)"}


def test_every_fixture_is_checked_and_free_of_values(fixture_json: Callable[[str], Any]) -> None:
    names = sorted(p.stem for p in (Path(__file__).resolve().with_name("fixtures")).glob("*.json"))
    assert names == sorted(FIXTURE_NAMES)

    def strings(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set().union(*(strings(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(strings(v) for v in value))
        return {value} if isinstance(value, str) else set()

    allowed = {
        "s",
        "User",
        "Organization",
        "public",
        "base64",
        "bearer",
        "authorization_pending",
        "slow_down",
        "Validation Failed",
        "selected",
        "modified",
        "blob",
        "commit",
        "closed",
        "unsigned",
        "NONE",
        "100644",
        "identical",
        "ahead",
        "added",
        "404",
        "422",
        "clean",
    }
    for name in names:
        assert strings(fixture_json(name)["body"]) <= allowed, name
