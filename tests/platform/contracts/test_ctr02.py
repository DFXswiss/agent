"""CON-01 (CTR-02): the guest control protocol, the journal, the session platform API, the hooks, the operation types,
the audit details and the model catalog are complete and strict against the tables of the specification.

Each table below quotes its source (03-api.md sections 7.1, 7.2, 8.1, 8.2, 9; 02-data-model.md sections 2.9 and 5;
05-runner.md sections 5.2, 5.3, 10.5). Fields marked as additions are decisions of CTR-02
(docs/platform/decisions/CTR-02.md).
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import pytest
from referencing._core import Resolver

from agent_hub.crypto import dedupe
from tests.platform.contracts.harness import (
    CONTRACTS,
    ID_BASE,
    Contracts,
    ecma_pattern,
    is_open_input,
    load_json,
    open_objects,
    schema_files,
    subschemas,
)

SCHEMAS = schema_files(CONTRACTS)


def fields(contracts: Contracts, rel: str, pointer: str = "") -> set[str]:
    """Property names an instance of the schema at `rel` (or `rel#pointer`) may have: union over `$ref`, `oneOf`,
    `anyOf`."""
    resolver = contracts.registry.resolver(base_uri=ID_BASE + rel)
    schema: Any = contracts.schemas[rel]
    if pointer:
        resolved = resolver.lookup(f"#{pointer}")
        schema, resolver = resolved.contents, resolved.resolver
    return _fields(schema, resolver)


def _fields(schema: Any, resolver: Resolver[Any]) -> set[str]:
    found: set[str] = set(schema.get("properties", {}))
    if "$ref" in schema:
        resolved = resolver.lookup(schema["$ref"])
        found |= _fields(resolved.contents, resolved.resolver)
    for key in ("oneOf", "anyOf"):
        for alternative in schema.get(key, []):
            found |= _fields(alternative, resolver)
    return found


def words(text: str) -> set[str]:
    return set(text.split())


# ---------------------------------------------------------------------------------------------- guest (03 7.1)
# 03-api.md section 7.1: endpoint -> request fields | response fields ("" = none; JSON body or query parameters).
SPEC_GUEST = {
    ("GET", "/v1/health"): ("", "guest_version claude_version state org_id wg_id sessions landlock_abi boot_id"),
    ("POST", "/v1/assign"): ("vm_id org_id wg_id", ""),
    ("POST", "/v1/sessions"): (
        # restore: contracts/changes/KOR-33.md
        "session_id task_id parent_session_id depth harness model resume_claude_session_id restore initial_prompt"
        " limits",
        "state",
    ),
    ("PUT", "/v1/sessions/{id}/credential"): (
        # access, models_allowed, budget: contracts/changes/GST-04.md
        "upstream_base_url auth_header auth_value extra_headers provider_allowlist access models_allowed budget",
        "",
    ),
    ("POST", "/v1/sessions/{id}/prompts"): ("prompt_id text speaker", "state"),  # speaker: ADR-083
    ("POST", "/v1/sessions/{id}/control"): ("action", ""),
    ("POST", "/v1/sessions/{id}/files/import"): ("dir replace", "files bytes"),
    ("POST", "/v1/sessions/{id}/files/export"): ("dir paths", "files skipped"),
    # context checkpoints: contracts/changes/KOR-33.md
    ("POST", "/v1/sessions/{id}/context/export"): (
        "workspace_max_bytes transcript_max_bytes",
        "v agent_kind agent_version claude_session_id session_id task_id cwd created_at transcript_files workspace"
        " workspace_files workspace_bytes",
    ),
    ("POST", "/v1/contexts/{id}/stage"): ("", "files bytes"),
    ("POST", "/v1/sessions/{id}/pings"): ("ping_id kind level ref", ""),
    ("GET", "/v1/journal"): ("after limit wait", "records head"),
    ("POST", "/v1/journal/ack"): ("through", ""),
    ("POST", "/v1/pending/{id}/answer"): ("status result error", ""),
    ("GET", "/v1/metrics"): ("", "vm sessions"),
}
# The export response schema describes the manifest member of the tar stream (decision), not a JSON body.
SPEC_GUEST_TAR = {("POST", "/v1/sessions/{id}/files/export"), ("POST", "/v1/sessions/{id}/context/export")}
# 03-api.md section 7.1, nested objects.
SPEC_GUEST_NESTED = {
    ("guest/sessions-create.request.json", "/properties/harness"): "context_markdown",
    ("guest/sessions-create.request.json", "/properties/model"): "access model_id env",
    ("guest/sessions-create.request.json", "/properties/initial_prompt"): "prompt_id text speaker",
    ("guest/sessions-create.request.json", "/properties/limits"): "memory_max cpu_quota tasks_max runtime_max_sec",
    ("guest/sessions-create.request.json", "/properties/restore"): "claude_session_id object_id note",
    ("guest/objects.json", "/$defs/vm_metrics"): "cpu_usage_usec mem_used_bytes mem_total_bytes disk_used_bytes"
    " disk_total_bytes net_rx_bytes net_tx_bytes uptime_seconds load1",
    # The optional counters after `state` were added by RUN-15 (contracts/changes/RUN-15.md), `sandbox_denials` by
    # KOR-60 (contracts/changes/KOR-60.md).
    ("guest/objects.json", "/$defs/session_metrics"): "session_id cpu_usage_usec mem_current_bytes mem_max_bytes"
    " tasks_current io_read_bytes io_write_bytes workspace_bytes state oom_kills hook_denies hook_errors"
    " sandbox_denials egress_denied"
    " model_input_tokens model_output_tokens model_request_bytes model_response_bytes",
}


@pytest.fixture(scope="module")
def guest_index() -> dict[str, Any]:
    document: dict[str, Any] = load_json(CONTRACTS / "guest" / "INDEX.json")
    return document


def test_guest_endpoints_match_the_specification(guest_index: dict[str, Any], contracts: Contracts) -> None:
    rows = guest_index["endpoints"]
    assert {(row["method"], row["path"]) for row in rows} == set(SPEC_GUEST)
    assert len(rows) == len(SPEC_GUEST)
    for row in rows:
        request, response = SPEC_GUEST[(row["method"], row["path"])]
        assert row["request"] == f"guest/{row['name']}.request.json"
        assert row["response"] == f"guest/{row['name']}.response.json"
        assert fields(contracts, row["request"]) == words(request), row["path"]
        assert fields(contracts, row["response"]) == words(response), row["path"]
        assert list(row["path_params"]) == re.findall(r"\{([a-z_]+)\}", row["path"])
        assert row["section"] == "7.1"
        assert (row["response_kind"] == "tar") is ((row["method"], row["path"]) in SPEC_GUEST_TAR)
        assert row["request_kind"] == ("query" if row["method"] == "GET" else row["request_kind"])
    assert guest_index["error_response"] in contracts.schemas
    on_disk = {rel for rel in SCHEMAS if rel.startswith("guest/") and rel.count("/") == 1}
    listed = {row[part] for row in rows for part in ("request", "response")}
    assert on_disk == listed | {"guest/objects.json", "guest/error.response.json"}


@pytest.mark.parametrize(("location", "expected"), list(SPEC_GUEST_NESTED.items()), ids=str)
def test_guest_nested_objects(contracts: Contracts, location: tuple[str, str], expected: str) -> None:
    assert fields(contracts, *location) == words(expected)


def test_guest_limits(contracts: Contracts) -> None:
    base = load_json(CONTRACTS / "fixtures/guest/sessions-create.request/valid/basic.json")
    for depth, ok in ((0, True), (2, True), (3, False), (True, False)):
        sample = copy.deepcopy(base)
        sample["depth"] = depth
        assert (contracts.errors("guest/sessions-create.request.json", sample) == []) is ok
    for wait, ok in (("0", True), ("25", True), ("26", False), ("025", False)):
        assert (contracts.errors("guest/journal.request.json", {"wait": wait}) == []) is ok


# ---------------------------------------------------------------------------------------------- journal (03 7.2)
# 03-api.md section 7.2 (fields of every body, variants merged) and 02-data-model.md section 5 (the kind list, which
# adds `metrics`; its body repeats GET /v1/metrics with a timestamp).
SPEC_JOURNAL = {
    "session.state": "state reason claude_session_id at prompt_id",
    "session.output": "message_id role text turn stop_reason",
    "session.tool": "tool_use_id name input_summary input result_summary is_error duration_ms",
    "session.hook": "event tool decision reason path",
    "session.file": "path op sha256 size patch",
    "session.terminal": "offset_ms_start frames cols rows",
    "session.tools_ready": "tools at",
    "usage": "request_id model provider input_tokens output_tokens cache_read_tokens cache_write_tokens"
    " thinking_tokens cost_usd context_window_tokens request_bytes response_bytes status",
    "operation.request": "pending_id operation_id type params idempotency_key requested_at",
    "platform.query": "pending_id tool arguments",
    # contracts/changes/KOR-29.md (13-work-vm.md section 3): a call of a work VM tool, answered by the runner
    "workvm.call": "pending_id tool arguments",
    "ping.delivered": "ping_id via",
    # contracts/changes/KOR-07.md: a read answered by the guest itself and an MCP request refused before forwarding
    "platform.read": "tool result_count",
    "platform.refused": "code tool method",
    "metrics": "at vm sessions",
    "guest.error": "code",
}
# Additions (decisions CTR-02): harness_version (05-runner.md section 8), truncated (64 Ki cut), rule (hook.denied).
JOURNAL_ADDITIONS = {
    "session.state": "harness_version",
    "session.output": "truncated",
    "session.hook": "rule",
    "usage": "provider_outside_allowlist",  # contracts/changes/GST-04.md
    "operation.request": "local_check local_error",  # contracts/changes/GST-07.md (05-runner.md section 12 step 1)
    "session.file": "files deleted bytes paths reason",  # contracts/changes/HOT-06.md (summary of many files)
}
SPEC_JOURNAL_RECORD = "seq ts session_id kind body"


def test_journal_kinds_match_the_specification(contracts: Contracts) -> None:
    index = load_json(CONTRACTS / "guest/journal/INDEX.json")
    assert [row["kind"] for row in index["kinds"]] == list(SPEC_JOURNAL)
    for row in index["kinds"]:
        kind = row["kind"]
        assert row["body"] == f"guest/journal/{kind}.json"
        assert fields(contracts, row["body"]) == words(SPEC_JOURNAL[kind]) | words(JOURNAL_ADDITIONS.get(kind, ""))
        assert row["vm_level"] is (kind in {"metrics", "guest.error"})
    assert fields(contracts, "guest/journal/record.json") == words(SPEC_JOURNAL_RECORD)
    on_disk = {rel for rel in SCHEMAS if rel.startswith("guest/journal/")}
    assert on_disk == {f"guest/journal/{kind}.json" for kind in SPEC_JOURNAL} | {"guest/journal/record.json"}


def test_refused_calls_name_only_the_tools_of_the_mcp_index() -> None:
    """`platform.refused` names a tool only from the closed list of the session platform API (KOR-07)."""
    tools = [row["name"].removeprefix("mcp__platform__") for row in load_json(CONTRACTS / "mcp/INDEX.json")["tools"]]
    variants = load_json(CONTRACTS / "guest/journal/record.json")["$defs"]["body_platform_refused"]["oneOf"]
    assert variants[1]["properties"]["tool"]["anyOf"][0]["enum"] == tools


def test_journal_record_binds_kind_and_body(contracts: Contracts) -> None:
    record = {
        "seq": 1,
        "ts": "2026-11-21T10:00:00.000Z",
        "session_id": None,
        "kind": "guest.error",
        "body": {"code": "gateway_route"},
    }
    contracts.validate("guest/journal/record.json", record)
    for kind, sample in (("ping.delivered", record["body"]), ("guest.error", {"ping_id": "x", "via": "hook"})):
        assert contracts.errors("guest/journal/record.json", {**record, "kind": kind, "body": sample})


def test_journal_failure_reasons_are_the_closed_list(contracts: Contracts) -> None:
    reasons = load_json(CONTRACTS / "enums/session-failure-reasons.json")["reasons"]
    for reason in reasons:
        contracts.validate(
            "guest/journal/session.state.json", {"state": "failed", "reason": reason, "at": "2026-11-21T10:00:00.000Z"}
        )


# ---------------------------------------------------------------------------------------------- MCP (03 8, 8.1)
# 03-api.md section 8.1: tool -> argument fields | result fields (list results are objects with a named array,
# decisions CTR-02: the named array is given after "=").
SPEC_TOOLS = {
    "get_context": ("", "org workgroup task session rights limits api_version"),
    "list_workgroups": ("", "workgroups=id,name,is_default,my_access"),
    "list_members": ("scope", "members=id,kind,display_name,role,access"),
    "list_tasks": ("state limit before", "tasks=id,title,state,owner_id,created_at,last_activity_at next_before"),
    "get_task": ("task_id", "id title state owner_id created_at sessions operations"),
    "get_task_messages": ("task_id after limit", "messages=seq,kind,author_id,ts,text next_after"),
    "get_whiteboard": ("level task_id", "entries=entry_id,author_id,created_at,updated_at,text"),
    "get_notifications": ("", "notifications=ping_id,kind,level,ref,at"),
    "request_operation": ("type params idempotency_key", "operation_id status result error duplicate_of"),
    "get_operation": ("operation_id wait_seconds", "operation_id status result error duplicate_of"),
    "list_operations": ("limit", "operations=operation_id,type,state,error_code,created_at"),
    "get_subagent_result": ("session_id wait_seconds", "session_id state final_text finished_at"),
    # 13-work-vm.md section 3 (contracts/changes/KOR-29.md): the work VM tools, answered through workvm.call
    "workvm_exec": (
        "command cwd env stdin timeout_seconds wait_seconds sync",
        "job_id command state exit_code output output_start output_bytes duration_ms synced",
    ),
    "workvm_output": (
        "job_id offset limit wait_seconds",
        "job_id command state exit_code output output_start output_bytes duration_ms synced",
    ),
    "workvm_kill": (
        "job_id signal",
        "job_id command state exit_code output output_start output_bytes duration_ms synced",
    ),
    "workvm_status": ("", "workvm jobs=job_id,command,state,exit_code,started_at,duration_ms"),
    "workvm_sync": ("direction path", "direction path files bytes deleted skipped"),
    "workvm_browse": ("url action selector text wait_ms path", "url final_url status title text screenshot job_id"),
}
WORKVM_TOOLS = frozenset(
    {"workvm_exec", "workvm_output", "workvm_kill", "workvm_status", "workvm_sync", "workvm_browse"}
)
SPEC_TOOL_STATUS = "succeeded failed rejected duplicate denied pending"
# 03-api.md section 8.1 and 12: upper limits of integer arguments (and the idempotency key length).
SPEC_TOOL_LIMITS = [
    ("list_tasks", "limit", 100),
    ("get_task_messages", "limit", 200),
    ("list_operations", "limit", 100),
    ("get_operation", "wait_seconds", 30),
    ("get_subagent_result", "wait_seconds", 30),
    ("workvm_exec", "wait_seconds", 30),
    ("workvm_exec", "timeout_seconds", 604800),
    ("workvm_output", "wait_seconds", 30),
    ("workvm_output", "limit", 32768),
    ("workvm_browse", "wait_ms", 30000),
]
TASK = "01a1ee56-5e00-7004-8f1b-340691d99f04"
REQUIRED_ARGS: dict[str, dict[str, object]] = {
    "get_task_messages": {"task_id": TASK},
    "get_operation": {"operation_id": TASK},
    "get_subagent_result": {"session_id": TASK},
    "workvm_exec": {"command": "true"},
    "workvm_output": {"job_id": "j-0123456789ab"},
    "workvm_browse": {"url": "https://example.com/"},
}


@pytest.fixture(scope="module")
def mcp_index() -> dict[str, Any]:
    document: dict[str, Any] = load_json(CONTRACTS / "mcp" / "INDEX.json")
    return document


def _result_fields(text: str) -> tuple[set[str], dict[str, set[str]]]:
    top: set[str] = set()
    nested: dict[str, set[str]] = {}
    for word in text.split():
        name, _, inner = word.partition("=")
        top.add(name)
        if inner:
            nested[name] = set(inner.split(","))
    return top, nested


def test_tools_match_the_specification(mcp_index: dict[str, Any], contracts: Contracts) -> None:
    assert [row["tool"] for row in mcp_index["tools"]] == list(SPEC_TOOLS)
    for row in mcp_index["tools"]:
        tool = row["tool"]
        arguments, result = SPEC_TOOLS[tool]
        assert row["name"] == f"mcp__platform__{tool}"
        assert fields(contracts, row["input"]) == words(arguments), tool
        top, nested = _result_fields(result)
        assert fields(contracts, row["output"]) == top, tool
        for name, inner in nested.items():
            assert fields(contracts, "mcp/objects.json", f"/$defs/output_{tool}/properties/{name}/items") == inner
        unaudited = tool == "request_operation" or tool in WORKVM_TOOLS
        assert row["read_audit"] == (None if unaudited else f"read.session.{tool}")
        assert row["via"] == ("workvm.call" if tool in WORKVM_TOOLS else row["via"])
    audit = {row["action"] for row in load_json(CONTRACTS / "enums/audit-actions.json")["actions"]}
    assert {row["read_audit"] for row in mcp_index["tools"] if row["read_audit"]} <= audit
    status: set[str] = set()
    for pointer, sub in subschemas(contracts.schemas["mcp/objects.json"]["$defs"]["operation_outcome"]):
        if pointer.endswith("/properties/status"):
            status |= set(sub.get("enum", [sub.get("const")]))
    assert status == words(SPEC_TOOL_STATUS)


# 03-api.md section 8.1 and section 9: nested objects of the results (location in the library -> fields).
SPEC_NESTED = {
    ("mcp/objects.json", "/$defs/output_get_context/properties/org"): "id name",
    ("mcp/objects.json", "/$defs/output_get_context/properties/workgroup"): "id name is_default",
    ("mcp/objects.json", "/$defs/output_get_context/properties/task"): "id title state owner",
    ("mcp/objects.json", "/$defs/output_get_context/properties/task/properties/owner"): "id display_name",
    ("mcp/objects.json", "/$defs/output_get_context/properties/session"): "id parent_session_id depth model_id",
    ("mcp/objects.json", "/$defs/output_get_context/properties/rights"): "workgroup",
    ("mcp/objects.json", "/$defs/output_get_context/properties/limits"): "subagent_max_depth subagent_max_parallel"
    " subagent_max_total",
    ("mcp/objects.json", "/$defs/output_get_task/properties/sessions/items"): "id state depth parent_session_id",
    ("mcp/objects.json", "/$defs/output_get_task/properties/operations/items"): "id type state",
    ("ops/objects.json", "/$defs/result_github_list_repositories/properties/repositories/items"): "repo default_branch"
    " private allowed",
    ("ops/objects.json", "/$defs/result_github_get_pull_request/properties/head"): "ref sha",
    ("ops/objects.json", "/$defs/result_github_get_pull_request/properties/base"): "ref sha",
    ("ops/objects.json", "/$defs/result_github_get_pull_request/properties/files/items"): "path status additions"
    " deletions patch",
    ("ops/objects.json", "/$defs/result_github_get_pull_request/properties/comments/items"): "id author body"
    " created_at",
}


@pytest.mark.parametrize(("location", "expected"), list(SPEC_NESTED.items()), ids=str)
def test_nested_result_fields(contracts: Contracts, location: tuple[str, str], expected: str) -> None:
    assert fields(contracts, *location) == words(expected)


def test_existing_pull_request_is_a_duplicate_without_original(contracts: Contracts) -> None:
    """03-api.md section 9: an open PR found by the executor ends succeeded with existing true; MCP says duplicate."""
    result = {"number": 7, "url": "https://github.example/p/7", "id": 77, "draft": False, "existing": True}
    sample = {"operation_id": TASK, "status": "duplicate", "result": result}
    contracts.validate("mcp/tools/request_operation.output.json", sample)


def test_tool_call_params_cover_every_tool(contracts: Contracts) -> None:
    alternatives = contracts.schemas["mcp/tools-call.params.json"]["oneOf"]
    assert [alt["properties"]["name"]["const"] for alt in alternatives] == list(SPEC_TOOLS)
    names = contracts.schemas["mcp/tools-list.result.json"]["properties"]["tools"]["items"]["properties"]["name"]
    assert names["enum"] == list(SPEC_TOOLS)


@pytest.mark.parametrize(("tool", "name", "maximum"), SPEC_TOOL_LIMITS)
def test_tool_integer_limits(contracts: Contracts, tool: str, name: str, maximum: int) -> None:
    rel = f"mcp/tools/{tool}.input.json"
    base = REQUIRED_ARGS.get(tool, {})
    contracts.validate(rel, {**base, name: maximum})
    for bad in (maximum + 1, -1, True, False, float(maximum), str(maximum)):
        assert contracts.errors(rel, {**base, name: bad}), (tool, name, bad)


def test_tool_arguments_are_closed_and_integers_strict(contracts: Contracts) -> None:
    for tool in SPEC_TOOLS:
        rel = f"mcp/tools/{tool}.input.json"
        for path in sorted((CONTRACTS / "fixtures" / rel.removesuffix(".json") / "valid").glob("*.json")):
            sample = load_json(path)
            assert contracts.errors(rel, {**sample, "extra": 1}), tool
            for key, value in sample.items():
                if type(value) is int:
                    assert contracts.errors(rel, {**sample, key: True}), (tool, key)


def test_tool_inputs_are_plain_objects(contracts: Contracts) -> None:
    """The model API refuses oneOf/anyOf/allOf at the top level of a tool input schema (GST-07 contract change)."""
    for tool in SPEC_TOOLS:
        definition = contracts.schemas["mcp/objects.json"]["$defs"][f"input_{tool}"]
        while "$ref" in definition and "type" not in definition:
            resolved = contracts.registry.resolver(base_uri=ID_BASE + "mcp/objects.json").lookup(definition["$ref"])
            definition = resolved.contents
        assert definition["type"] == "object", tool
        assert definition["additionalProperties"] is False, tool
        assert not {"oneOf", "anyOf", "allOf"} & set(definition), tool


def test_whiteboard_task_id_rule_is_left_to_the_implementations(contracts: Contracts) -> None:
    """03-api.md section 8.1 `get_whiteboard {level, task_id?}`: the schema accepts every level with an optional task
    ID; `task_id` exactly for level task is checked by the guest and the runner."""
    rel = "mcp/tools/get_whiteboard.input.json"
    for level in ("org", "workgroup", "task"):
        contracts.validate(rel, {"level": level})
        contracts.validate(rel, {"level": level, "task_id": TASK})
    assert contracts.errors(rel, {"level": "device"})
    assert contracts.errors(rel, {"task_id": TASK})


def test_idempotency_key_length(contracts: Contracts) -> None:
    rel = "mcp/tools/request_operation.input.json"
    base = {"type": "task.set_title", "params": {"title": "x"}}
    contracts.validate(rel, {**base, "idempotency_key": "k" * 128})
    assert contracts.errors(rel, {**base, "idempotency_key": "k" * 129})
    assert contracts.errors(rel, {**base, "idempotency_key": ""})


def test_mcp_methods_and_server(mcp_index: dict[str, Any], contracts: Contracts) -> None:
    """03-api.md section 8: methods, server name, default protocol version."""
    assert [row["method"] for row in mcp_index["methods"]] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "ping",
    ]
    assert mcp_index["server"] == {
        "name": "platform",
        "server_info_name": "agent-dfx-swiss-platform",
        "default_protocol_version": "2025-06-18",
        "endpoint": "http://127.0.0.1:7700/mcp",
    }
    result = contracts.schemas["mcp/initialize.result.json"]
    assert result["properties"]["serverInfo"]["properties"]["name"]["const"] == "agent-dfx-swiss-platform"
    for row in mcp_index["methods"]:
        for part in ("params", "result"):
            assert row[part] is None or row[part] in contracts.schemas
    for part in mcp_index["envelope"].values():
        assert part in contracts.schemas
    errors = contracts.resolve("mcp/rpc-error.json", contracts.schemas["mcp/rpc-error.json"]["$ref"])
    assert isinstance(errors, dict)
    assert -32601 in errors["properties"]["code"]["enum"]


# ---------------------------------------------------------------------------------------------- hooks (03 8.2)
# 05-runner.md section 5.2: the 18 hook events.
SPEC_HOOK_EVENTS = (
    "SessionStart SessionEnd UserPromptSubmit PreToolUse PostToolUse PostToolUseFailure PermissionRequest"
    " PermissionDenied Notification Stop StopFailure SubagentStart SubagentStop PreCompact PostCompact ConfigChange"
    " InstructionsLoaded CwdChanged"
)
# 03-api.md section 8.2, verbatim.
SPEC_PROMPT_BLOCK = "Prompts are accepted only from agent.dfx.swiss."
SPEC_NOTICE = (
    "agent.dfx.swiss notice: <n> new item(s) available (<kinds>). Use mcp__platform__get_notifications to see them."
)
# 05-runner.md section 5.3 rule 2, verbatim.
SPEC_TOOL_FORBIDDEN = (
    "Tool <name> is forbidden in agent.dfx.swiss sessions. Request actions through the platform tools "
    "(mcp__platform__request_operation)."
)
NOTICE_EVENTS = {"PreToolUse", "PostToolUse", "UserPromptSubmit"}
PING_KINDS = ["whiteboard.entry", "subagent.finished", "operation.finished"]


@pytest.fixture(scope="module")
def deny() -> dict[str, Any]:
    document: dict[str, Any] = load_json(CONTRACTS / "mcp" / "hook-deny-reasons.json")
    return document


def test_hook_events(mcp_index: dict[str, Any], contracts: Contracts) -> None:
    events = SPEC_HOOK_EVENTS.split()
    assert [row["event"] for row in mcp_index["hooks"]] == events
    for event in events:
        request = contracts.schemas[f"mcp/hooks/{event}.request.json"]
        assert request["properties"]["hook_event_name"]["const"] == event
        response = f"mcp/hooks/{event}.response.json"
        notice = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": _notice(1, ["whiteboard.entry"])}}
        if event == "PreToolUse":
            notice["hookSpecificOutput"]["permissionDecision"] = "allow"
        else:
            contracts.validate(response, {})
        assert (contracts.errors(response, notice) == []) is (event in NOTICE_EVENTS), event
    journal_events = contracts.schemas["guest/journal/record.json"]["$defs"]["body_session_hook"]["properties"]
    assert journal_events["event"]["enum"] == events


def test_hook_requests_have_no_upper_limits(contracts: Contracts) -> None:
    """A long field must not make the guest refuse a hook body (fail open, 05-runner.md section 5.5)."""
    for event in SPEC_HOOK_EVENTS.split():
        for pointer, sub in subschemas(contracts.schemas[f"mcp/hooks/{event}.request.json"]):
            assert "maxLength" not in sub, (event, pointer)
            assert "maxItems" not in sub, (event, pointer)
    big = load_json(CONTRACTS / "fixtures/mcp/hooks/UserPromptSubmit.request/valid/basic.json")
    contracts.validate("mcp/hooks/UserPromptSubmit.request.json", {**big, "prompt": "x" * (2**20 + 1)})


def _notice(n: int, kinds: list[str]) -> str:
    return SPEC_NOTICE.replace("<n>", str(n)).replace("<kinds>", ", ".join(kinds))


@pytest.mark.parametrize(
    ("n", "kinds", "ok"),
    [
        (1, ["whiteboard.entry"], True),
        (3, PING_KINDS, True),
        (999999, ["operation.finished"], True),
        (0, ["whiteboard.entry"], False),
        (1, [], False),
        (1, ["chat"], False),
        (1, [*PING_KINDS, "whiteboard.entry"], False),
        (2, ["whiteboard.entry", "whiteboard.entry"], False),
        (2, ["operation.finished", "whiteboard.entry"], False),
        (2, ["whiteboard.entry", "operation.finished"], True),
        (2, ["subagent.finished", "operation.finished"], True),
    ],
)
def test_notice_text(contracts: Contracts, n: int, kinds: list[str], ok: bool) -> None:
    sample = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": _notice(n, kinds)}}
    assert (contracts.errors("mcp/hooks/PostToolUse.response.json", sample) == []) is ok


def test_deny_reason_file_matches_the_schema(deny: dict[str, Any], contracts: Contracts) -> None:
    reasons = deny["pre_tool_use"]
    assert {row["rule"] for row in reasons} == {1, 2, 3, 4, 5, 6}  # rule 1: question_passed (KOR-39)
    assert "input_invalid" in {row["id"] for row in reasons}
    assert len({row["id"] for row in reasons}) == len(reasons)
    fixed = [row["text"] for row in reasons if "text" in row]
    schema = contracts.schemas["mcp/objects.json"]["$defs"]["deny_reason"]
    assert sorted(schema["anyOf"][0]["enum"]) == sorted(fixed)  # same sentences; the file lists rule 1 first
    template = next(row for row in reasons if "template" in row)
    assert template["template"] == SPEC_TOOL_FORBIDDEN.replace("<name>", "{name}")
    rel = "mcp/hooks/PreToolUse.response.json"
    for name, ok in (("Bash", True), ("mcp__evil__x", True), ("a b", False), ("", False), ("x" * 129, False)):
        text = template["template"].format(name=name)
        assert (re.fullmatch(template["name_pattern"].strip("^$"), name) is not None) is ok
        sample = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": text,
            }
        }
        assert (contracts.errors(rel, sample) == []) is ok, name
    for text in fixed:
        assert re.fullmatch(r"[ -~]+", text)
        assert text.endswith(".")
    assert deny["user_prompt_submit"]["text"] == SPEC_PROMPT_BLOCK
    block = contracts.schemas["mcp/hooks/UserPromptSubmit.response.json"]["oneOf"][1]
    assert block["properties"]["reason"]["const"] == SPEC_PROMPT_BLOCK
    assert deny["notice"]["template"] == SPEC_NOTICE.replace("<n>", "{n}").replace("<kinds>", "{kinds}")
    assert deny["notice"]["kinds_order"] == PING_KINDS
    assert set(deny["notice"]["events"]) == NOTICE_EVENTS


# ---------------------------------------------------------------------------------------------- operations (03 9)
# 03-api.md section 9: type -> params | result fields | dedupe fields (as normalised by 04-crypto.md section 4.10 and
# agent_hub.crypto.dedupe) | window in seconds.
SPEC_OPS: dict[str, tuple[str, str, str, int | None]] = {
    "github.list_repositories": ("github_identity", "repositories", "", None),
    "github.checkout": ("repo ref github_identity", "repo commit_sha dir files bytes", "repo ref session", 600),
    "github.get_pull_request": (
        "repo number github_identity",
        "number title body state draft mergeable mergeable_state head base url files comments truncated",
        "",
        None,
    ),
    "github.create_branch": ("repo branch from github_identity", "branch sha", "repo branch", 86400),
    "github.commit": (
        "repo branch message paths github_identity author committer co_authors signing merge decisions",
        "commit_sha parent_sha tree_sha files_changed merge_parent",
        "repo branch parent_sha tree_sha",
        86400,
    ),
    "github.push": ("repo branch commit_sha github_identity", "branch sha url", "repo branch commit_sha", 86400),
    "github.create_pull_request": (
        "repo head base title body draft github_identity",
        "number url id draft existing",
        "repo head base",
        604800,
    ),
    "github.comment": ("repo number body github_identity", "comment_id url", "repo number body", 86400),
    "github.list_issues": (
        "repo state labels sort direction per_page page github_identity",
        "issues next_page truncated note",
        "",
        None,
    ),
    "github.get_issue": (
        "repo number comments_page comments_per_page github_identity",
        "number title body state state_reason author labels assignees created_at updated_at closed_at url"
        " pull_request comments_total comments comments_next_page truncated",
        "",
        None,
    ),
    "github.create_issue": ("repo title body labels github_identity", "number url id", "repo title body", 86400),
    "github.update_issue": (
        "repo number state state_reason title body labels github_identity",
        "number url state state_reason title labels",
        "",
        None,
    ),
    "github.list_pull_requests": (
        "repo state base sort direction per_page page github_identity",
        "pull_requests next_page truncated",
        "",
        None,
    ),
    "github.request_connection": ("", "login", "", None),
    "subagent.request": ("purpose prompt model_id github_identity", "session_id", "session prompt", 600),
    "task.set_title": ("title", "", "", None),
    # 13-work-vm.md section 2 (contracts/changes/KOR-29.md)
    "workvm.request": (
        "size scope",
        "workvm_id state scope size cpus memory_mib disk_gib created ready_ms",
        "",
        None,
    ),
    "workvm.release": ("scope", "released workvm_id", "", None),
    # KOR-43 (ADR-080 section 1; 13-work-vm.md section 12)
    "test.run": (
        "kind command cwd timeout_seconds size",
        "run_id state exit_code duration_ms ready_ms summary log tail reason",
        "",
        None,
    ),
    # KOR-39 (ADR-076 section 3)
    "human.question": ("question", "", "", None),
    "human.approval": ("question", "", "", None),
    # ADR-078 (contracts/changes/KOR-47.md)
    "github.list_identities": ("", "acting_user current identities co_authors", "", None),
    "github.merge_base": (
        "repo number discard github_identity",
        "repo number branch base head_sha base_sha merge_base_sha up_to_date conflicts conflicts_total dir files bytes",
        "",
        None,
    ),
}
SPEC_REPO = "^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"
# 03-api.md section 9: (type, field, longest valid value length).
SPEC_TEXT_LIMITS = [
    ("github.commit", "message", 4096),
    ("github.create_pull_request", "title", 256),
    ("github.create_pull_request", "body", 65536),
    ("github.comment", "body", 65536),
    ("subagent.request", "purpose", 200),
    ("subagent.request", "prompt", 32768),
    ("task.set_title", "title", 120),
    ("human.question", "question", 4000),
    ("human.approval", "question", 4000),
]
SPEC_PROTECTED_DEFAULT: list[str] = []


@pytest.fixture(scope="module")
def ops_index() -> dict[str, Any]:
    document: dict[str, Any] = load_json(CONTRACTS / "ops" / "INDEX.json")
    return document


@pytest.fixture(scope="module")
def rules() -> dict[str, Any]:
    document: dict[str, Any] = load_json(CONTRACTS / "ops" / "rules.json")
    return document


def test_operation_types_match_the_specification(ops_index: dict[str, Any], contracts: Contracts) -> None:
    assert [row["type"] for row in ops_index["types"]] == list(SPEC_OPS)
    for row in ops_index["types"]:
        params, result, dedupe_fields, _ = SPEC_OPS[row["type"]]
        assert row["params"] == f"ops/{row['type']}.params.json"
        assert row["result"] == f"ops/{row['type']}.result.json"
        assert fields(contracts, row["params"]) == words(params), row["type"]
        assert fields(contracts, row["result"]) == words(result), row["type"]
        assert row["dedupe"] is bool(dedupe_fields)
    on_disk = {rel for rel in SCHEMAS if rel.startswith("ops/")}
    listed = {row[part] for row in ops_index["types"] for part in ("params", "result")}
    assert on_disk == listed | {"ops/objects.json", "ops/error.json"}
    assert ops_index["error"] == "ops/error.json"
    result = contracts.schemas["ops/objects.json"]["$defs"]["result"]["anyOf"]
    assert len(result) == len(SPEC_OPS)
    assert contracts.schemas["common.json"]["$defs"]["operation_type"]["pattern"]
    for op_type in SPEC_OPS:
        assert ecma_pattern(contracts.schemas["common.json"]["$defs"]["operation_type"]["pattern"]).search(op_type)


def test_dedupe_fields(contracts: Contracts) -> None:
    document = load_json(CONTRACTS / "ops" / "dedupe-fields.json")
    expected = {t: (words(f), w) for t, (_, _, f, w) in SPEC_OPS.items() if f}
    found = {t: ({f["name"] for f in row["fields"]}, row["window_seconds"]) for t, row in document["types"].items()}
    assert found == expected
    assert set(found) == set(dedupe.DEDUPE_TYPES)
    vectors = load_json(CONTRACTS / "vectors" / "dedupe.json")["tags"]
    for vector in vectors:
        if vector.get("type") in found and "idempotency_key" not in vector["fields"]:
            assert set(vector["fields"]) == found[vector["type"]][0], vector["type"]
    assert {f["name"] for f in document["idempotency_key"]["fields"]} == {"idempotency_key", "task"}
    assert document["types"]["github.create_pull_request"]["existing_pull_request_check"] is True


def test_repo_rule(contracts: Contracts, rules: dict[str, Any]) -> None:
    assert rules["repo"]["pattern"] == SPEC_REPO
    assert contracts.schemas["ops/objects.json"]["$defs"]["repo"]["pattern"] == SPEC_REPO
    for value in rules["repo"]["valid"]:
        assert ecma_pattern(SPEC_REPO).search(value), value
    for value in rules["repo"]["invalid"]:
        assert not ecma_pattern(SPEC_REPO).search(value), value


def branch_ok(rule: dict[str, Any], name: str) -> bool:
    """Reference reading of ops/rules.json#/branch (what RUN-07 implements)."""
    name = name.removeprefix(rule["strip_prefix"])
    return (
        0 < len(name) <= rule["max_length"]
        and not any(ord(c) < 0x20 or ord(c) == 0x7F or c in " ~^:?*[\\" for c in name)
        and not any(s in name for s in rule["forbidden_substrings"])
        and not name.startswith(tuple(rule["forbidden_prefixes"]))
        and not name.endswith(tuple(rule["forbidden_suffixes"]))
        and name not in rule["forbidden_names"]
        and not any(part.endswith(tuple(rule["forbidden_component_suffixes"])) for part in name.split("/"))
    )


def test_branch_rules(contracts: Contracts, rules: dict[str, Any]) -> None:
    rule = rules["branch"]
    pattern = ecma_pattern(contracts.schemas["ops/objects.json"]["$defs"]["branch"]["pattern"])
    limit = contracts.schemas["ops/objects.json"]["$defs"]["branch"]["maxLength"]
    assert limit == rule["max_length"] == 200
    for name in rule["valid"]:
        assert branch_ok(rule, name), name
        assert pattern.search(name), name
    for name in rule["invalid"]:
        assert not branch_ok(rule, name), name
    # The schema already refuses the forbidden characters and first characters.
    for name in ("a b", "a~1", "a:b", "a\\b", "a[b", "-x", "/x", ".x", "a\u0007b"):
        assert not pattern.search(name), name
    ref = rules["ref"]
    for value in ref["valid"]:
        assert branch_ok(rule, value) or re.fullmatch(ref["sha_pattern"].strip("^$"), value)
    for value in ref["invalid"]:
        assert not branch_ok(rule, value)
    assert rules["protected_branches"]["default"] == SPEC_PROTECTED_DEFAULT
    for example in rules["protected_branches"]["examples"]:
        regex = re.escape(example["pattern"]).replace(r"\*", "[^/]*")
        assert (re.fullmatch(regex, example["branch"]) is not None) is example["protected"], example
        assert fnmatch.fnmatchcase(example["branch"], example["pattern"]) or not example["protected"]


def path_ok(rule: dict[str, Any], path: str) -> bool:
    """Reference reading of ops/rules.json#/path."""
    segments = path.split("/")
    return (
        0 < len(path) <= rule["max_length"]
        and not any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in path)
        and not path.startswith("/")
        and not any(s in rule["forbidden_segments"] for s in segments)
        and not {seg.casefold() for seg in segments} & set(rule["forbidden_segments_casefold"])
    )


def test_path_rules(rules: dict[str, Any]) -> None:
    rule = rules["path"]
    for path in rule["valid"]:
        assert path_ok(rule, path), path
    for path in rule["invalid"]:
        assert not path_ok(rule, path), path


def test_secret_patterns(rules: dict[str, Any]) -> None:
    patterns = rules["secret_patterns"]["patterns"]
    assert [p["id"] for p in patterns] == [
        "pem_private_key",
        "github_token",
        "github_pat",
        "anthropic_key",
        "openrouter_key",
        "aws_access_key",
    ]
    for entry in patterns:
        regex = re.compile(entry["pattern"])
        assert entry["positive"]
        assert entry["negative"]
        for parts in entry["positive"]:
            assert regex.search("".join(parts)), entry["id"]
        for parts in entry["negative"]:
            assert not regex.search("".join(parts)), entry["id"]


def test_limits_in_rules_match_the_schemas(contracts: Contracts, rules: dict[str, Any]) -> None:
    assert rules["commit_limits"] == {
        "file_bytes": 10 * 2**20,
        "files": 500,
        "total_bytes": 50 * 2**20,
        "message_max_length": 4096,
    }
    assert rules["checkout_limits"] == {"archive_bytes": 512 * 2**20}
    assert rules["subagent_limits"]["max_depth"] == 2
    assert rules["subagent_limits"]["max_running_per_task"] == 3
    assert rules["subagent_limits"]["max_total_per_task"] == 10
    paths = contracts.schemas["ops/objects.json"]["$defs"]["params_github_commit"]["properties"]["paths"]
    assert paths["maxItems"] == rules["commit_limits"]["files"]


@pytest.mark.parametrize(("op_type", "name", "longest"), SPEC_TEXT_LIMITS)
def test_text_limits(contracts: Contracts, op_type: str, name: str, longest: int) -> None:
    rel = f"ops/{op_type}.params.json"
    base = load_json(CONTRACTS / "fixtures" / rel.removesuffix(".json") / "valid" / "basic.json")
    contracts.validate(rel, {**base, name: "x" * longest})
    assert contracts.errors(rel, {**base, name: "x" * (longest + 1)})
    assert contracts.errors(rel, {**base, name: 7})


def test_integer_params_reject_booleans(contracts: Contracts) -> None:
    for op_type in SPEC_OPS:
        rel = f"ops/{op_type}.params.json"
        base = load_json(CONTRACTS / "fixtures" / rel.removesuffix(".json") / "valid" / "basic.json")
        for key, value in base.items():
            if type(value) is int:
                assert contracts.errors(rel, {**base, key: True}), (op_type, key)
                assert contracts.errors(rel, {**base, key: 0}), (op_type, key)


# ---------------------------------------------------------------------------------------------- audit (02 2.9)
GENERIC_DETAIL = ("api/org-audit.response.json", "/properties/rows/items/properties/detail")


def test_every_audit_action_has_a_detail_schema() -> None:
    actions = [row["action"] for row in load_json(CONTRACTS / "enums/audit-actions.json")["actions"]]
    on_disk = sorted(path.stem for path in (CONTRACTS / "audit").glob("*.json"))
    assert on_disk == sorted(actions)
    for action in actions:
        assert f"audit/{action}.json" in SCHEMAS


def test_audit_details_fit_the_generic_row(contracts: Contracts) -> None:
    """The per-action schemas only narrow the generic `detail` of the audit row (flat, scalar, printable)."""
    rel, pointer = GENERIC_DETAIL
    resolver = contracts.registry.resolver(base_uri=ID_BASE + rel)
    generic = resolver.lookup(f"#{pointer}").contents
    validator = contracts.validator(rel).evolve(schema=generic)
    for path in sorted((CONTRACTS / "fixtures" / "audit").rglob("valid/*.json")):
        validator.validate(load_json(path))
    for name, schema in SCHEMAS.items():
        if name.startswith("audit/"):
            assert "detail" in schema["description"]
            for pointer_, sub in subschemas(schema):
                assert sub.get("type") != "array", (name, pointer_)
                if pointer_.startswith("/properties/") and pointer_.count("/") == 2:
                    assert sub.get("type") != "object", (name, pointer_)


def test_audit_detail_never_names_paths_or_parameters(contracts: Contracts) -> None:
    for name, schema in SCHEMAS.items():
        if name.startswith("audit/"):
            assert not {"path", "params", "body", "text", "prompt", "title", "name"} & set(schema.get("properties", {}))


def test_credential_provisioning_is_strict(contracts: Contracts) -> None:
    rel = "guest/session-credential.request.json"
    base = load_json(CONTRACTS / "fixtures/guest/session-credential.request/valid/basic.json")
    for url, ok in (
        ("https://x.example", True),
        ("http://127.0.0.1:20170/v1", True),
        ("http://localhost", True),
        ("http://x.example", False),
        ("http://127.0.0.1.example", False),
        ("ftp://x", False),
    ):
        assert (contracts.errors(rel, {**base, "upstream_base_url": url}) == []) is ok, url
    for name, ok in (("x-title", True), ("Authorization", False), ("x\r\ny", False), ("", False)):
        sample = {**base, "extra_headers": [{"name": name, "value": "v"}]}
        assert (contracts.errors(rel, sample) == []) is ok, name
    reserved = load_json(CONTRACTS / "guest/INDEX.json")["reserved_headers"]
    assert {"authorization", "x-api-key", "host"} <= set(reserved)


# ---------------------------------------------------------------------------------------------- model catalog
SPEC_CATALOG_DEFAULT = "deepseek/deepseek-v4.1-flash-1017"
SPEC_ACCESS = ["platform", "api_key", "subscription"]


def test_model_catalog_example(contracts: Contracts) -> None:
    example = load_json(CONTRACTS / "model-catalog.example.json")
    assert "$schema" not in example
    contracts.validate("model-catalog.json", example)
    assert list(example["access"]) == SPEC_ACCESS
    assert example["access"]["platform"]["default"] == SPEC_CATALOG_DEFAULT
    model_id = ecma_pattern(contracts.schemas["common.json"]["$defs"]["model_id"]["pattern"])
    for model in example["models"]:
        assert model_id.search(model), model
    for access in example["access"].values():
        assert set(access["allowed"]) <= set(example["models"])
        assert access["default"] is None or access["default"] in access["allowed"]
    assert set(contracts.schemas["common.json"]["$defs"]["model_access"]["enum"]) == set(SPEC_ACCESS)
    config = contracts.schemas["api/config.response.json"]["properties"]["models"]["properties"]
    assert list(config) == SPEC_ACCESS


# ---------------------------------------------------------------------------------------------- strictness
ALLOWED_OPEN = {
    **{f"mcp/hooks/{event}.request.json": ["/"] for event in SPEC_HOOK_EVENTS.split()},
    "mcp/initialize.params.json": ["/properties/clientInfo"],
}


def test_open_objects_only_for_third_party_input() -> None:
    found = {rel: open_objects(schema) for rel, schema in SCHEMAS.items() if open_objects(schema)}
    assert found == ALLOWED_OPEN


@pytest.mark.parametrize(
    ("schema", "is_open"),
    [
        ({"type": "object", "$comment": "open: x", "properties": {}, "additionalProperties": {}}, True),
        ({"type": "object", "$comment": "x", "properties": {}, "additionalProperties": {}}, False),
        ({"type": "object", "$comment": "open: x", "properties": {}, "additionalProperties": True}, False),
        ({"type": "object", "properties": {}, "additionalProperties": {}}, False),
    ],
)
def test_open_marker(schema: dict[str, Any], is_open: bool) -> None:
    assert is_open_input(schema) is is_open


def test_ctr02_schemas_have_fixtures_for_every_message() -> None:
    for rel in SCHEMAS:
        if rel.split("/")[0] in {"guest", "mcp", "ops", "audit"} or rel == "model-catalog.json":
            if rel.endswith("objects.json"):
                continue
            base = CONTRACTS / "fixtures" / rel.removesuffix(".json")
            assert list((base / "valid").glob("*.json")), rel
            assert list((base / "invalid").glob("*.json")), rel


def test_data_files_are_not_schemas() -> None:
    for name in (
        "guest/INDEX.json",
        "guest/journal/INDEX.json",
        "mcp/INDEX.json",
        "mcp/hook-deny-reasons.json",
        "ops/INDEX.json",
        "ops/dedupe-fields.json",
        "ops/rules.json",
        "model-catalog.example.json",
    ):
        document = json.loads(Path(CONTRACTS / name).read_text(encoding="utf-8"))
        assert isinstance(document, dict), name
        assert "$schema" not in document, name
        assert name not in SCHEMAS
