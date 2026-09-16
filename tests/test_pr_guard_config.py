"""Schema coverage for optional environment-deployment approval."""

from __future__ import annotations

import json

import pytest

from agent_cli.pr_guard_config import PrGuardConfigError, load_pr_guard_config

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


def test_duplicate_json_key_fails_closed() -> None:
    raw = (
        '{"schema":"pr-guard/v1",'
        '"a38":{"enforce":[],"exclude":[],"default":"enforce"},'
        '"environment_approval":{"enabled":true,"enabled":false,'
        '"environment":"pr-ci","workflows":[".github/workflows/pr.yml"]}}'
    )
    with pytest.raises(PrGuardConfigError, match="duplicate key"):
        load_pr_guard_config(raw)
