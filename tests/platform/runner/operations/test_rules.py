"""U-RUN-operations: the formal rule primitives equal `contracts/ops/rules.json` and pass its positive and negative
examples (03-api.md section 9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.operations import rules

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
RULES: dict[str, Any] = json.loads((CONTRACTS / "ops" / "rules.json").read_text(encoding="utf-8"))


def test_constants_equal_the_contract() -> None:
    assert rules.REPO_RE.pattern == RULES["repo"]["pattern"]
    branch = RULES["branch"]
    assert branch["max_length"] == rules.BRANCH_MAX
    assert list(rules.BRANCH_FORBIDDEN_SUBSTRINGS) == branch["forbidden_substrings"]
    assert list(rules.BRANCH_FORBIDDEN_PREFIXES) == branch["forbidden_prefixes"]
    assert list(rules.BRANCH_FORBIDDEN_SUFFIXES) == branch["forbidden_suffixes"]
    assert list(rules.BRANCH_FORBIDDEN_COMPONENT_SUFFIXES) == branch["forbidden_component_suffixes"]
    assert list(rules.BRANCH_FORBIDDEN_NAMES) == branch["forbidden_names"]
    assert branch["strip_prefix"] == rules.HEADS
    assert rules.SHA_RE.pattern == RULES["ref"]["sha_pattern"]
    assert list(rules.DEFAULT_PROTECTED) == RULES["protected_branches"]["default"]
    path = RULES["path"]
    assert path["max_length"] == rules.PATH_MAX
    assert list(rules.PATH_FORBIDDEN_SEGMENTS) == path["forbidden_segments"]
    assert list(rules.PATH_FORBIDDEN_SEGMENTS_CASEFOLD) == path["forbidden_segments_casefold"]
    assert RULES["commit_limits"] == rules.COMMIT_LIMITS
    assert RULES["checkout_limits"] == rules.CHECKOUT_LIMITS
    assert RULES["pull_request_limits"] == rules.PULL_REQUEST_LIMITS
    assert RULES["comment_limits"] == rules.COMMENT_LIMITS
    assert RULES["subagent_limits"] == rules.SUBAGENT_LIMITS
    assert RULES["title_limits"] == rules.TITLE_LIMITS
    assert [list(item) for item in rules.SECRET_PATTERNS] == [
        [item["id"], item["pattern"]] for item in RULES["secret_patterns"]["patterns"]
    ]


@pytest.mark.parametrize("value", RULES["repo"]["valid"])
def test_valid_repositories(value: str) -> None:
    assert rules.repo_valid(value)


@pytest.mark.parametrize("value", [*RULES["repo"]["invalid"], 7, None])
def test_invalid_repositories(value: object) -> None:
    assert not rules.repo_valid(value)


@pytest.mark.parametrize("value", [*RULES["branch"]["valid"], "refs/heads/feature/x"])
def test_valid_branches(value: str) -> None:
    assert rules.branch_valid(value)


@pytest.mark.parametrize("value", [*RULES["branch"]["invalid"], "a\x7fb", "refs/heads/", 3])
def test_invalid_branches(value: object) -> None:
    assert not rules.branch_valid(value)


def test_branch_names_lose_refs_heads() -> None:
    assert rules.branch_name("refs/heads/feature/x") == "feature/x"
    assert rules.branch_name("feature/x") == "feature/x"


@pytest.mark.parametrize("value", RULES["ref"]["valid"])
def test_valid_refs(value: str) -> None:
    assert rules.ref_valid(value)


@pytest.mark.parametrize("value", [*RULES["ref"]["invalid"], None])
def test_invalid_refs(value: object) -> None:
    assert not rules.ref_valid(value)


@pytest.mark.parametrize("value", RULES["path"]["valid"])
def test_valid_paths(value: str) -> None:
    assert rules.path_valid(value)


@pytest.mark.parametrize("value", [*RULES["path"]["invalid"], "a" * 4097, 5])
def test_invalid_paths(value: object) -> None:
    assert not rules.path_valid(value)


@pytest.mark.parametrize("example", RULES["protected_branches"]["examples"])
def test_protected_branch_examples(example: dict[str, Any]) -> None:
    assert rules.protected(example["branch"], [example["pattern"]]) is example["protected"]


def test_protected_branches_match_literally_and_after_refs_heads() -> None:
    assert rules.protected("refs/heads/main", rules.DEFAULT_PROTECTED)
    assert not rules.protected("mainXline", ["main.line"])  # "." is literal
    assert rules.protected("v1+2", ["v1+2"])
    assert not rules.protected("anything", [])


@pytest.mark.parametrize("value", ["Fix the login", "ü" * 120, "a"])
def test_valid_titles(value: str) -> None:
    assert rules.title_valid(value)


@pytest.mark.parametrize(
    "value", ["", "a" * 121, "two\nlines", "tab\tx", "x\u2028y", "x\u2029y", "x\x7f", "next\x85line", "x\x9f", None]
)
def test_invalid_titles(value: object) -> None:
    assert not rules.title_valid(value)


def _join(parts: list[str]) -> str:
    return "".join(parts)


@pytest.mark.parametrize(
    ("name", "text"),
    [(item["id"], _join(example)) for item in RULES["secret_patterns"]["patterns"] for example in item["positive"]],
)
def test_secret_patterns_find_the_positive_examples(name: str, text: str) -> None:
    assert rules.find_secret(text) == name
    assert rules.find_secret("prefix\n" + text + "\nsuffix") == name


@pytest.mark.parametrize(
    "text",
    [_join(example) for item in RULES["secret_patterns"]["patterns"] for example in item["negative"]],
)
def test_secret_patterns_ignore_the_negative_examples(text: str) -> None:
    assert rules.find_secret(text) is None
