"""The static harness text images/harness/RULES.md (docs/platform/spec/05-runner.md section 8, 03-api.md
sections 8.1 and 9): every mandatory statement and every operation type with a parameter example."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES = REPO_ROOT / "images" / "harness" / "RULES.md"

# 03-api.md section 9: parameters (required, optional) of every operation type.
SPEC_OPERATIONS: dict[str, tuple[set[str], set[str]]] = {
    "github.list_repositories": (set(), set()),
    "github.checkout": ({"repo"}, {"ref"}),
    "github.get_pull_request": ({"repo", "number"}, set()),
    "github.create_branch": ({"repo", "branch", "from"}, set()),
    "github.commit": ({"repo", "branch", "message"}, {"paths"}),
    "github.push": ({"repo", "branch", "commit_sha"}, set()),
    "github.create_pull_request": ({"repo", "head", "base", "title"}, {"body", "draft"}),
    "github.comment": ({"repo", "number", "body"}, set()),
    "subagent.request": ({"purpose", "prompt"}, {"model_id"}),
    "task.set_title": ({"title"}, set()),
}
MANDATORY = [
    "agent.dfx.swiss",
    "You must not run programs, scripts, shell commands, git commands, tests or builds yourself",
    "You must not start subagents yourself.",
    "You must not modify, disable or bypass hooks, settings, this harness or the sandbox",
    "You may read files in your task workspace",
    "You may create, write and edit files only inside your task workspace",
    "Request every other action with `mcp__platform__request_operation`",
    "Subagents exist only through `subagent.request`",
    "content-free hint",
    "Use `mcp__platform__get_notifications`",
    "Every blocked attempt",
    "is recorded",
]


@pytest.fixture(scope="module")
def text() -> str:
    return RULES.read_text(encoding="utf-8")


def examples(text: str) -> list[dict[str, Any]]:
    found = [json.loads(match) for match in re.findall(r"`(\{\"type\": .*?\})`$", text, re.MULTILINE)]
    assert found
    return found


@pytest.mark.parametrize("statement", MANDATORY)
def test_mandatory_statement(text: str, statement: str) -> None:
    assert statement in text


def test_every_operation_type_has_exactly_one_example(text: str) -> None:
    assert [example["type"] for example in examples(text)] == list(SPEC_OPERATIONS)


@pytest.mark.parametrize("operation", list(SPEC_OPERATIONS))
def test_example_parameters_follow_the_operation_rules(text: str, operation: str) -> None:
    (example,) = [example for example in examples(text) if example["type"] == operation]
    assert set(example) == {"type", "params"}
    required, optional = SPEC_OPERATIONS[operation]
    assert required <= set(example["params"]) <= required | optional
    repo = example["params"].get("repo")
    if repo is not None:
        assert re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}", repo)


def test_examples_validate_against_the_operation_contracts(text: str) -> None:
    ops = REPO_ROOT / "contracts" / "ops"
    assert ops.is_dir(), "contracts/ops is missing: run scripts/sync-contracts.sh <agent-core checkout>"
    # Contract schemas reference each other by relative paths inside contracts/: register every file by its URI.
    registry: Registry[Any] = Registry().with_resources(
        (path.as_uri(), Resource.from_contents(json.loads(path.read_text(encoding="utf-8")), DRAFT202012))
        for path in (REPO_ROOT / "contracts").rglob("*.json")
    )
    for example in examples(text):
        schema = {"$ref": (ops / f"{example['type']}.params.json").as_uri()}
        jsonschema.Draft202012Validator(schema, registry=registry).validate(example["params"])


def test_names_every_read_tool_of_the_platform_api(text: str) -> None:
    for tool in (
        "get_context",
        "list_workgroups",
        "list_members",
        "list_tasks",
        "get_task",
        "get_task_messages",
        "get_whiteboard",
        "get_notifications",
        "get_operation",
        "list_operations",
        "get_subagent_result",
    ):
        assert f"`{tool}`" in text or f"`mcp__platform__{tool}`" in text, tool


def test_lists_every_operation_status(text: str) -> None:
    for status in ("succeeded", "failed", "rejected", "duplicate", "denied", "pending"):
        assert f"`{status}`" in text
