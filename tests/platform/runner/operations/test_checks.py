"""U-RUN-operations: formal checks of every operation type, positive and negative, with exact codes (03-api.md
section 9; `contracts/ops/<type>.params.json`)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_runner.model.selection import load_catalog
from agent_runner.operations import checks
from agent_runner.operations.extensions import CheckFailed
from agent_runner.operations.settings import DEFAULTS, WorkgroupSettings
from agent_runner.sessions.records import LocalSession

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
OBJECTS: dict[str, Any] = json.loads((CONTRACTS / "ops" / "objects.json").read_text(encoding="utf-8"))["$defs"]
INDEX: dict[str, Any] = json.loads((CONTRACTS / "ops" / "INDEX.json").read_text(encoding="utf-8"))
REPO = "Example-Org/Example-Repo"
SHA = "a" * 40
SESSION = LocalSession(
    id="01930005-0000-7000-8000-000000000001",
    task_id="01930004-0000-7000-8000-000000000001",
    org_id="01930001-0000-7000-8000-000000000001",
    wg_id="01930002-0000-7000-8000-000000000001",
    vm_id="vm-00000001",
    parent_id=None,
    depth=0,
    state="working",
    claude_session_id=None,
    model_access="platform",
    model_id="deepseek/deepseek-v4.1-flash-1017",
    owner_id="01930003-0000-7000-8000-000000000001",
    created_at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC),
    ready_at=None,
    ended_at=None,
)
SETTINGS = WorkgroupSettings(
    repositories=frozenset({"example-org/example-repo"}),
    protected_branches=("main", "release/*"),
    pr_draft_default=False,
)
VALID: dict[str, dict[str, Any]] = {
    "github.list_repositories": {},
    "github.checkout": {"repo": REPO, "ref": "v1.2.3"},
    "github.get_pull_request": {"repo": REPO, "number": 7},
    "github.create_branch": {"repo": REPO, "branch": "feature/x", "from": "main"},
    "github.commit": {"repo": REPO, "branch": "feature/x", "message": "Fix it.", "paths": ["src/app.py"]},
    "github.push": {"repo": REPO, "branch": "feature/x", "commit_sha": SHA},
    "github.create_pull_request": {"repo": REPO, "head": "feature/x", "base": "main", "title": "Fix", "body": ""},
    "github.comment": {"repo": REPO, "number": 7, "body": "Looks good."},
    "subagent.request": {"purpose": "Review", "prompt": "Review PR 7.", "model_id": "deepseek/deepseek-v4-pro"},
    "task.set_title": {"title": "Fix the login"},
}


def request(op_type: str, params: dict[str, Any], **changes: Any) -> checks.CheckRequest:
    base = checks.CheckRequest(
        op_type=op_type,
        params=params,
        session=SESSION,
        settings=SETTINGS,
        catalog=load_catalog(None),
        connected=True,
        task_commits=frozenset({SHA}),
    )
    return replace(base, **changes)


def refused(op_type: str, params: dict[str, Any], **changes: Any) -> CheckFailed:
    with pytest.raises(CheckFailed) as caught:
        checks.check(request(op_type, params, **changes))
    return caught.value


def with_(op_type: str, **changes: Any) -> dict[str, Any]:
    params = {**VALID[op_type], **changes}
    return {k: v for k, v in params.items() if v is not None}


def test_the_rule_table_covers_every_type_with_the_contract_keys() -> None:
    assert set(checks.TYPES) == {entry["type"] for entry in INDEX["types"]}
    for op_type, rule in checks.TYPES.items():
        schema = OBJECTS["params_" + op_type.replace(".", "_")]
        if "$ref" in schema:  # list_repositories: the empty object
            assert rule.fields == {}
            assert rule.required == ()
            continue
        assert set(rule.fields) == set(schema["properties"]), op_type
        assert set(rule.required) == set(schema["required"]), op_type


@pytest.mark.parametrize("op_type", sorted(VALID))
def test_every_type_accepts_valid_parameters(op_type: str) -> None:
    assert checks.check(request(op_type, VALID[op_type])) == checks.effective(op_type, VALID[op_type], SETTINGS)


def test_unknown_types_are_refused() -> None:
    failed = refused("github.delete_repo", {})
    assert failed.code == "unknown_type"
    assert failed.error() == {"code": "unknown_type", "message": "The operation type is not known."}


@pytest.mark.parametrize(
    ("op_type", "params", "pointers"),
    [
        ("github.list_repositories", {"x": 1}, ["/params/x"]),
        ("github.checkout", {}, ["/params/repo"]),
        ("github.checkout", with_("github.checkout", repo="agent"), ["/params/repo"]),
        ("github.checkout", with_("github.checkout", ref="a..b"), ["/params/ref"]),
        ("github.get_pull_request", with_("github.get_pull_request", number=0), ["/params/number"]),
        ("github.get_pull_request", with_("github.get_pull_request", number=True), ["/params/number"]),
        ("github.get_pull_request", with_("github.get_pull_request", number=2**31), ["/params/number"]),
        ("github.get_pull_request", with_("github.get_pull_request", number="7"), ["/params/number"]),
        ("github.create_branch", with_("github.create_branch", branch="x.lock"), ["/params/branch"]),
        ("github.create_branch", with_("github.create_branch", **{"from": "-x"}), ["/params/from"]),
        ("github.commit", with_("github.commit", message=""), ["/params/message"]),
        ("github.commit", with_("github.commit", message="m" * 4097), ["/params/message"]),
        ("github.commit", with_("github.commit", paths=[]), ["/params/paths"]),
        ("github.commit", with_("github.commit", paths=["a"] * 501), ["/params/paths"]),
        ("github.commit", with_("github.commit", paths=[3]), ["/params/paths"]),
        ("github.commit", with_("github.commit", paths="src/app.py"), ["/params/paths"]),
        ("github.push", with_("github.push", commit_sha="A" * 40), ["/params/commit_sha"]),
        ("github.push", with_("github.push", commit_sha="abc1234"), ["/params/commit_sha"]),
        ("github.create_pull_request", with_("github.create_pull_request", title=""), ["/params/title"]),
        ("github.create_pull_request", with_("github.create_pull_request", title="t" * 257), ["/params/title"]),
        ("github.create_pull_request", with_("github.create_pull_request", body="b" * 65537), ["/params/body"]),
        ("github.create_pull_request", with_("github.create_pull_request", draft="yes"), ["/params/draft"]),
        ("github.create_pull_request", with_("github.create_pull_request", base="a b"), ["/params/base"]),
        ("github.comment", with_("github.comment", body=""), ["/params/body"]),
        ("github.comment", with_("github.comment", body="b" * 65537), ["/params/body"]),
        ("subagent.request", with_("subagent.request", purpose="p" * 201), ["/params/purpose"]),
        ("subagent.request", with_("subagent.request", prompt=""), ["/params/prompt"]),
        ("subagent.request", with_("subagent.request", prompt="p" * 32769), ["/params/prompt"]),
        ("subagent.request", with_("subagent.request", model_id="-bad"), ["/params/model_id"]),
        ("task.set_title", {"title": ""}, ["/params/title"]),
        ("task.set_title", {"title": "t" * 121}, ["/params/title"]),
        ("task.set_title", {"title": "two\nlines"}, ["/params/title"]),
        ("task.set_title", {"title": "Fix", "description": "x"}, ["/params/description"]),
        ("github.comment", {"repo": 1, "number": 7, "body": "x", "a/b": 1}, ["/params/a~1b", "/params/repo"]),
    ],
)
def test_parameters_are_checked_with_pointers(op_type: str, params: dict[str, Any], pointers: list[str]) -> None:
    failed = refused(op_type, params)
    assert failed.code == "params_invalid"
    assert failed.pointers == pointers
    assert failed.error()["pointers"] == pointers


def test_at_most_32_pointers_are_reported() -> None:
    params = {f"k{n:02d}": n for n in range(40)}
    failed = refused("task.set_title", params)
    assert len(failed.pointers or []) == 32


def test_lengths_are_counted_in_code_points() -> None:
    checks.check(request("task.set_title", {"title": "ü" * 120}))
    checks.check(request("github.comment", with_("github.comment", body="€" * 65536)))
    assert refused("task.set_title", {"title": "ü" * 121}).code == "params_invalid"


@pytest.mark.parametrize("path", ["../x", "a/../b", ".git/config", "sub/.GIT/x", "/etc/passwd", "a//b", "a\\b", "./a"])
def test_commit_paths_are_path_invalid(path: str) -> None:
    failed = refused("github.commit", with_("github.commit", paths=["ok.txt", path]))
    assert (failed.code, failed.pointers) == ("path_invalid", ["/params/paths/1"])


@pytest.mark.parametrize("op_type", [t for t in VALID if t.startswith("github.") and t != "github.list_repositories"])
def test_repositories_outside_the_allow_list_are_refused(op_type: str) -> None:
    assert refused(op_type, with_(op_type, repo="other/repo")).code == "repo_not_allowed"


@pytest.mark.parametrize("op_type", [t for t in VALID if t.startswith("github.") and t != "github.list_repositories"])
def test_without_an_allow_list_every_repository_passes(op_type: str) -> None:
    # KOR-30: no settings event, or an event with an empty list, restricts nothing (GitHub is the boundary)
    empty = replace(SETTINGS, repositories=frozenset())
    for settings in (DEFAULTS, empty):
        assert not settings.restricted
        assert checks.check(request(op_type, with_(op_type, repo="other/repo"), settings=settings))["repo"] == (
            "other/repo"
        )
    assert SETTINGS.restricted


def test_the_allow_list_is_compared_lower_case() -> None:
    checks.check(request("github.comment", with_("github.comment", repo="EXAMPLE-ORG/example-REPO")))


def test_listing_repositories_needs_no_allow_list() -> None:
    assert checks.check(request("github.list_repositories", {})) == {}
    assert checks.check(request("github.list_repositories", {}, settings=DEFAULTS)) == {}


@pytest.mark.parametrize(
    ("op_type", "field", "value"),
    [
        ("github.create_branch", "branch", "main"),
        ("github.create_branch", "branch", "refs/heads/release/2.0"),
        ("github.commit", "branch", "main"),
        ("github.push", "branch", "release/1"),
        ("github.create_pull_request", "head", "main"),
    ],
)
def test_protected_branches_are_refused(op_type: str, field: str, value: str) -> None:
    assert refused(op_type, with_(op_type, **{field: value})).code == "branch_protected"


def test_a_protected_base_or_a_nested_release_branch_is_fine() -> None:
    checks.check(request("github.create_pull_request", with_("github.create_pull_request", base="main")))
    checks.check(request("github.create_branch", with_("github.create_branch", branch="release/1/hotfix")))


@pytest.mark.parametrize("op_type", [t for t in VALID if t.startswith("github.")])
def test_github_types_need_the_owners_connection(op_type: str) -> None:
    assert refused(op_type, VALID[op_type], connected=False).code == "github_not_connected"


@pytest.mark.parametrize("op_type", ["subagent.request", "task.set_title"])
def test_other_types_need_no_github_connection(op_type: str) -> None:
    checks.check(request(op_type, VALID[op_type], connected=False))


def test_push_needs_a_commit_of_this_task() -> None:
    failed = refused("github.push", VALID["github.push"], task_commits=frozenset({"b" * 40}))
    assert failed.code == "not_found"


def test_subagent_models_come_from_the_catalog_of_the_access_kind() -> None:
    checks.check(request("subagent.request", with_("subagent.request", model_id=None)))
    other = replace(SESSION, model_access="subscription")
    assert refused("subagent.request", VALID["subagent.request"], session=other).code == "model_not_allowed"
    unknown = replace(SESSION, model_access=None)
    assert refused("subagent.request", VALID["subagent.request"], session=unknown).code == "model_not_allowed"
    params = with_("subagent.request", model_id="claude-opus-5-5")
    assert refused("subagent.request", params).code == "model_not_allowed"


def test_effective_parameters_normalise_branches_and_take_the_draft_default() -> None:
    params = with_("github.create_pull_request", head="refs/heads/feature/x", base="refs/heads/develop")
    found = checks.check(request("github.create_pull_request", params))
    assert (found["head"], found["base"], found["draft"]) == ("feature/x", "develop", False)
    params = with_("github.create_pull_request", draft=True)
    assert checks.check(request("github.create_pull_request", params))["draft"] is True
    found = checks.check(request("github.push", with_("github.push", branch="refs/heads/feature/x")))
    assert found["branch"] == "feature/x"


def test_check_order_gives_the_first_failing_code() -> None:
    # params before the allow-list, the allow-list before protection, protection before the connection
    bad = with_("github.create_branch", repo="other/repo", branch="a..b")
    assert refused("github.create_branch", bad).code == "params_invalid"
    bad = with_("github.create_branch", repo="other/repo", branch="main")
    assert refused("github.create_branch", bad, connected=False).code == "repo_not_allowed"
    bad = with_("github.create_branch", branch="main")
    assert refused("github.create_branch", bad, connected=False).code == "branch_protected"


def test_check_failures_build_the_error_object() -> None:
    assert CheckFailed("x").error() == {"code": "upstream_error"}  # nothing outside the closed list
    assert CheckFailed("timeout", message="non-ASCII ü").error() == {"code": "timeout"}
    failed = CheckFailed("params_invalid", pointers=[f"/p/{n}" for n in range(40)], message="M.")
    assert failed.error() == {"code": "params_invalid", "message": "M.", "pointers": [f"/p/{n}" for n in range(32)]}
