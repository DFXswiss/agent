"""`list_operations {limit? <= 100}` (03-api.md section 8.1): the operations of the session's task that this device
registered (`runner.operation`, newest first) with state, error code and creation time. Workgroup level.

Operations of the task requested on another device (a task moved between devices) are not listed: the hub's
`GET /v1/device/tasks/{task}` has no creation time; `get_task` lists every operation of the task.
"""

from __future__ import annotations

from typing import Any

from agent_runner.queries import common, device_api
from agent_runner.queries.rights import read_scope
from agent_runner.sessions.records import time_text
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

DEFAULT_LIMIT, MAX_LIMIT = 50, 100
ERROR_CODES = frozenset(
    {
        "unknown_type",
        "params_invalid",
        "repo_not_allowed",
        "repo_not_accessible",
        "branch_protected",
        "branch_exists",
        "non_fast_forward",
        "path_invalid",
        "secret_detected",
        "size_exceeded",
        "github_not_connected",
        "not_found",
        "limit_exceeded",
        "budget_exceeded",
        "credit_exhausted",
        "rights_denied",
        "hub_unreachable",
        "upstream_error",
        "timeout",
        "model_not_allowed",
        "tree_mismatch",
        "no_changes",
        "workvm_unavailable",
        "identity_forbidden",
        "identity_unavailable",
        "conflict_markers",
        "conflicts_unresolved",
        "merge_outdated",
        "merge_pending",
        "merge_unsupported",
        "workspace_dirty",
        "device_failed",
    }
)
"""`contracts/enums/operation-error-codes.json` (a test compares them)."""


async def list_operations(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        limit = common.integer(common.arguments(call, frozenset({"limit"})), "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
        scope = read_scope(call.context, call.session)
        scope.require_workgroup()
        async with common.store(call.context) as (conn, _key):
            cur = await conn.execute(
                "SELECT id::text, type, state, error_code, created_at FROM runner.operation"
                " WHERE task_id = %s AND wg_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
                (call.session.task_id, scope.wg_id, limit),
            )
            rows = await cur.fetchall()
        found: list[dict[str, Any]] = [
            {
                "operation_id": op_id,
                "type": op_type,
                "state": state,
                "error_code": code if code in ERROR_CODES else None,
                "created_at": time_text(created.timestamp()),
            }
            for op_id, op_type, state, code, created in rows
            if device_api.OPERATION_TYPE_RE.fullmatch(op_type) and state in device_api.OPERATION_STATES
        ]
        return common.ok({"operations": found}, len(found), target_kind="task", target_id=call.session.task_id)

    return await common.answered(read)


TOOL = list_operations
