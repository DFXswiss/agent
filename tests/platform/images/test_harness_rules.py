"""The static harness text images/harness/RULES.md (docs/platform/spec/05-runner.md section 8, 03-api.md
sections 8.1 and 9, 13-work-vm.md section 9): every mandatory statement and every operation type with a parameter
example (KOR-25: the issue types, the pull-request list, the connection request and the rule to request GitHub
operations even without a connection)."""

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
    "github.list_issues": ({"repo"}, {"state", "labels", "sort", "direction", "per_page", "page"}),
    "github.get_issue": ({"repo", "number"}, {"comments_page", "comments_per_page"}),
    "github.create_issue": ({"repo", "title"}, {"body", "labels"}),
    "github.update_issue": ({"repo", "number"}, {"state", "state_reason", "title", "body", "labels"}),
    "github.list_pull_requests": ({"repo"}, {"state", "base", "sort", "direction", "per_page", "page"}),
    "github.request_connection": (set(), set()),
    "subagent.request": ({"purpose", "prompt"}, {"model_id"}),
    "task.set_title": ({"title"}, set()),
    "workvm.request": (set(), {"size", "scope"}),
    "workvm.release": (set(), {"scope"}),
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
    "If the task needs GitHub, request the operation",
    "even when GitHub is not connected; the platform asks the user to connect",
    "continues your operation afterwards.",
    "Do not explain what is missing and do not offer workarounds.",
    "If no other type fits yet, request `github.request_connection`.",
    "comment on an issue or a pull request",
    "An answer `pending` for a GitHub operation means the platform is waiting for the task owner",
    "wait for it with get_operation and do not request it again or explain what is missing.",
    'The newest issue is the first entry of `{"repo": "example-org/example-repo"}`',
    "content-free hint",
    "Use `mcp__platform__get_notifications`",
    "Every blocked attempt",
    "is recorded",
    # 13-work-vm.md section 9
    "You have your own work VM for everything that needs execution",
    "Never say that you cannot execute",
    "You run nothing in your own sandbox.",
    "this is the platform's controlled interface",
    "`workvm_exec` and `workvm_browse` obtain a work VM automatically when you have none.",
    "type `workvm.request` only for a larger `size`",
    "with `scope` `session` when you are a subagent that needs its own VM",
    "The work VM is temporary",
    "after 30 minutes without use and when the session ends",
    "Request a new one any time",
    "Files that must survive belong in your workspace",
    "Run tests (lint, prettier, unit and integration tests) with `mcp__platform__workvm_exec`.",
    "result, its log and its exit code are kept and visible to the user.",
    "Use the browser with `mcp__platform__workvm_browse`",
    "Root, internet access, installing packages and starting services and containers are allowed there",
    "allowed there without asking.",
    "`mcp__platform__workvm_sync` direction `from_vm`",
    "only through the GitHub operations",
    "The work VM holds no platform credentials. Credentials you choose to use there are your own responsibility.",
    "type `workvm.release`",
]
FORBIDDEN = ["say so in your answer", "Request it once", "deleted with the task anyway"]


@pytest.fixture(scope="module")
def text() -> str:
    return RULES.read_text(encoding="utf-8")


def examples(text: str) -> list[dict[str, Any]]:
    found = [json.loads(match) for match in re.findall(r"`(\{\"type\": .*?\})`$", text, re.MULTILINE)]
    assert found
    return found


@pytest.mark.parametrize("statement", MANDATORY)
def test_mandatory_statement(text: str, statement: str) -> None:
    # line breaks of the Markdown source are ordinary spaces
    assert statement in " ".join(text.split())


@pytest.mark.parametrize("statement", FORBIDDEN)
def test_no_statement_tells_the_session_to_give_up(text: str, statement: str) -> None:
    assert statement not in text


def test_every_operation_type_has_exactly_one_example(text: str) -> None:
    assert [example["type"] for example in examples(text)] == list(SPEC_OPERATIONS)


def test_the_operation_list_is_the_contract_index() -> None:
    index = json.loads((REPO_ROOT / "contracts" / "ops" / "INDEX.json").read_text(encoding="utf-8"))
    assert [entry["type"] for entry in index["types"]] == list(SPEC_OPERATIONS)


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
    assert ops.is_dir(), "contracts/ops is missing from the checkout"
    # Contract schemas reference each other by relative paths inside contracts/: register every file by its URI.
    registry: Registry[Any] = Registry().with_resources(
        (path.as_uri(), Resource.from_contents(json.loads(path.read_text(encoding="utf-8")), DRAFT202012))
        for path in (REPO_ROOT / "contracts").rglob("*.json")
    )
    for example in examples(text):
        schema = {"$ref": (ops / f"{example['type']}.params.json").as_uri()}
        jsonschema.Draft202012Validator(schema, registry=registry).validate(example["params"])


def test_names_every_tool_of_the_platform_api(text: str) -> None:
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
        "workvm_exec",
        "workvm_output",
        "workvm_kill",
        "workvm_status",
        "workvm_sync",
        "workvm_browse",
    ):
        assert f"`{tool}`" in text or f"`mcp__platform__{tool}`" in text, tool


def test_lists_every_operation_status(text: str) -> None:
    for status in ("succeeded", "failed", "rejected", "duplicate", "denied", "pending"):
        assert f"`{status}`" in text
