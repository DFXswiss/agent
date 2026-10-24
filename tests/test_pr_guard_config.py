"""Schema coverage for optional environment-deployment approval."""

from __future__ import annotations

import json

import pytest

from agent_cli.pr_guard_config import (
    PrGuardConfigError,
    PrGuardLookupError,
    fetch_trusted_pr_guard,
    load_pr_guard_config,
    one_review_for,
)

pytestmark = pytest.mark.no_pg

PATH = ".github/workflows/pr.yml"
BASE = {
    "schema": "pr-guard/v1",
    "a38": {"enforce": [], "exclude": [], "default": "enforce"},
}


def _config(environment_approval: object) -> dict:
    return {**BASE, "environment_approval": environment_approval}


def test_omitted_environment_approval_is_not_normalized() -> None:
    config = load_pr_guard_config(json.dumps(BASE))
    assert "environment_approval" not in config


def test_inclusive_environment_approval_bounds_are_accepted() -> None:
    long_environment = "e" * 255
    long_path = ".github/workflows/" + ("p" * 233) + ".yml"
    paths = [f".github/workflows/w{i}.yml" for i in range(64)]
    paths[0] = long_path
    config = load_pr_guard_config(
        json.dumps(
            _config(
                {
                    "enabled": True,
                    "environment": long_environment,
                    "workflows": paths,
                }
            )
        )
    )
    assert config["environment_approval"]["environment"] == long_environment
    assert config["environment_approval"]["workflows"] == paths


def test_valid_enabled_environment_approval_is_normalized() -> None:
    config = load_pr_guard_config(
        json.dumps(
            _config(
                {
                    "enabled": True,
                    "environment": "pr-ci",
                    "workflows": [PATH],
                }
            )
        )
    )
    assert config["environment_approval"] == {
        "enabled": True,
        "environment": "pr-ci",
        "workflows": [PATH],
    }


def test_disabled_environment_approval_allows_empty_workflows() -> None:
    config = load_pr_guard_config(
        json.dumps(
            _config(
                {"enabled": False, "environment": "pr-ci", "workflows": []}
            )
        )
    )
    assert config["environment_approval"] == {
        "enabled": False,
        "environment": "pr-ci",
        "workflows": [],
    }


@pytest.mark.parametrize(
    "approval",
    [
        {"environment": "pr-ci", "workflows": [PATH]},
        {"enabled": True, "workflows": [PATH]},
        {"enabled": True, "environment": "pr-ci"},
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [PATH],
            "extra": True,
        },
        {"enabled": "true", "environment": "pr-ci", "workflows": [PATH]},
        {"enabled": 1, "environment": "pr-ci", "workflows": [PATH]},
        {"enabled": True, "environment": "", "workflows": [PATH]},
        {"enabled": True, "environment": "x" * 256, "workflows": [PATH]},
        {"enabled": True, "environment": "pr-ci", "workflows": []},
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [PATH, PATH],
        },
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [".github/workflows/*.yml"],
        },
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [".github/workflows/pr-?.yml"],
        },
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [".github/workflows/pr[12].yml"],
        },
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [".github/workflows/../pr.yml"],
        },
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [".github/workflows/nested/pr.yml"],
        },
        [PATH],
        {
            "enabled": True,
            "environment": ["pr-ci"],
            "workflows": [PATH],
        },
        {"enabled": True, "environment": "pr-ci", "workflows": PATH},
        {"enabled": True, "environment": "pr-ci", "workflows": [1]},
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [f".github/workflows/w{i}.yml" for i in range(65)],
        },
        {
            "enabled": True,
            "environment": "pr-ci",
            "workflows": [PATH + ("x" * 240)],
        },
        {"enabled": False, "environment": "pr-ci", "workflows": [PATH, PATH]},
    ],
)
def test_invalid_environment_approval_fails_closed(approval: object) -> None:
    with pytest.raises(PrGuardConfigError):
        load_pr_guard_config(json.dumps(_config(approval)))


def test_unknown_top_level_key_fails_closed() -> None:
    payload = _config(
        {"enabled": True, "environment": "pr-ci", "workflows": [PATH]}
    )
    payload["extra"] = True
    with pytest.raises(PrGuardConfigError):
        load_pr_guard_config(json.dumps(payload))


def test_omitted_one_review_authors_keeps_the_two_review_standard() -> None:
    config = load_pr_guard_config(json.dumps(BASE))
    assert "one_review_authors" not in config
    assert one_review_for(config, "alice") is False
    assert one_review_for(None, "alice") is False
    assert one_review_for(config, "") is False


def test_empty_one_review_authors_is_valid_and_is_not_an_exception() -> None:
    config = load_pr_guard_config(json.dumps({**BASE, "one_review_authors": []}))
    assert config["one_review_authors"] == []
    assert one_review_for(config, "alice") is False


def test_one_review_authors_match_the_opener_case_insensitively() -> None:
    config = load_pr_guard_config(
        json.dumps({**BASE, "one_review_authors": ["Alice", "bob-1"]})
    )
    assert config["one_review_authors"] == ["alice", "bob-1"]
    assert one_review_for(config, "ALICE") is True
    assert one_review_for(config, "Bob-1") is True
    assert one_review_for(config, "carol") is False


def test_one_review_authors_rejects_bad_logins_and_duplicates() -> None:
    login_39 = "a" * 39
    accepted = load_pr_guard_config(
        json.dumps({**BASE, "one_review_authors": [login_39]})
    )
    assert accepted["one_review_authors"] == [login_39]
    rejected = (
        ["-alice"],
        ["alice-"],
        ["ali--ce"],
        ["alice_bob"],
        ["a" * 40],
        [""],
        [1],
        "alice",
        ["Alice", "alice"],
        ["a"] * 65,
    )
    for authors in rejected:
        with pytest.raises(PrGuardConfigError):
            load_pr_guard_config(json.dumps({**BASE, "one_review_authors": authors}))


def test_duplicate_json_key_fails_closed() -> None:
    raw = (
        '{"schema":"pr-guard/v1",'
        '"a38":{"enforce":[],"exclude":[],"default":"enforce"},'
        '"environment_approval":{"enabled":true,"enabled":false,'
        '"environment":"pr-ci","workflows":[".github/workflows/pr.yml"]}}'
    )
    with pytest.raises(PrGuardConfigError, match="duplicate key"):
        load_pr_guard_config(raw)


class _Completed:
    def __init__(self, code: int, out: str, err: str = "") -> None:
        self.returncode = code
        self.stdout = out
        self.stderr = err


def test_fetch_trusted_pr_guard_reads_the_default_branch_commit() -> None:
    seen: list[list[str]] = []
    body = json.dumps({**BASE, "one_review_authors": ["Alice"]})
    sha = "a" * 40

    def runner(argv: list[str]) -> _Completed:
        seen.append(argv)
        path = argv[-1]
        if path == "repos/acme/widget":
            return _Completed(0, json.dumps({"default_branch": "develop"}))
        if path == "repos/acme/widget/commits/develop":
            return _Completed(0, json.dumps({"sha": sha}))
        if path == f"repos/acme/widget/contents/.github/pr-guard.json?ref={sha}":
            return _Completed(0, body)
        raise AssertionError(argv)

    config = fetch_trusted_pr_guard(runner, "acme/widget")
    assert one_review_for(config, "alice") is True
    assert all("refs/heads/" not in " ".join(argv) for argv in seen)
    assert any(f"ref={sha}" in argv[-1] for argv in seen)


def test_fetch_trusted_pr_guard_duplicate_one_review_authors_is_not_an_exception() -> None:
    raw = (
        '{"schema":"pr-guard/v1",'
        '"a38":{"enforce":[],"exclude":[],"default":"enforce"},'
        '"one_review_authors":[],'
        '"one_review_authors":["alice"]}'
    )
    sha = "c" * 40

    def runner(argv: list[str]) -> _Completed:
        path = argv[-1]
        if path == "repos/acme/widget":
            return _Completed(0, json.dumps({"default_branch": "develop"}))
        if path == "repos/acme/widget/commits/develop":
            return _Completed(0, json.dumps({"sha": sha}))
        if path == f"repos/acme/widget/contents/.github/pr-guard.json?ref={sha}":
            return _Completed(0, raw)
        raise AssertionError(argv)

    config = fetch_trusted_pr_guard(runner, "acme/widget")
    assert config is None
    assert one_review_for(config, "alice") is False


def test_fetch_trusted_pr_guard_missing_file_is_not_an_exception() -> None:
    def runner(argv: list[str]) -> _Completed:
        path = argv[-1]
        if path == "repos/acme/widget":
            return _Completed(0, json.dumps({"default_branch": "develop"}))
        if path.endswith("/commits/develop"):
            return _Completed(0, json.dumps({"sha": "b" * 40}))
        return _Completed(1, "", "HTTP 404: Not Found")

    assert fetch_trusted_pr_guard(runner, "acme/widget") is None
    assert one_review_for(None, "alice") is False


def test_fetch_trusted_pr_guard_sha_containing_404_is_not_a_missing_file() -> None:
    sha = ("b" * 37) + "404"

    def runner(argv: list[str]) -> _Completed:
        path = argv[-1]
        if path == "repos/acme/widget":
            return _Completed(0, json.dumps({"default_branch": "develop"}))
        if path.endswith("/commits/develop"):
            return _Completed(0, json.dumps({"sha": sha}))
        return _Completed(
            1,
            "",
            f"HTTP 500 for repos/acme/widget/contents/.github/pr-guard.json?ref={sha}",
        )

    with pytest.raises(PrGuardLookupError):
        fetch_trusted_pr_guard(runner, "acme/widget")


def test_fetch_trusted_pr_guard_transport_error_is_not_a_missing_file() -> None:
    def runner(argv: list[str]) -> _Completed:
        return _Completed(1, "", "connection reset")

    with pytest.raises(PrGuardLookupError):
        fetch_trusted_pr_guard(runner, "acme/widget")
