"""Executor of `task.mode` (`<area>/executors.py:EXECUTORS`; ADR-098 section 3, way `sentence`).

The pre-check proved the quote; the executor tells the hub the outcome in plaintext metadata only - the mode and on
or off - with `PUT /v1/device/tasks/{task}/modes/{mode}` `{on, operation_id}`; the quote stays sealed in the
operation's parameters. The hub records it with the operation's `triggered_by` as chooser. Its refusals become the
operation's error: `404` (the hub does not offer the mode, or the operation is no longer open) `not_found`, `403`
`rights_denied` (the instruction's author may not write), `409 mode_needs` `mode_needs` with the missing modes named;
an unreachable hub is retried (`hub_unreachable`). The result is `{mode, on}`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import SyncRuntime, runtime

CODES = {403: "rights_denied", 404: "not_found"}


def target(task_id: str, mode: str) -> str:
    return f"/v1/device/tasks/{task_id}/modes/{mode}"


def _needs_message(detail: dict[str, object]) -> str:
    names = detail.get("modes")
    named = ", ".join(str(name) for name in names) if isinstance(names, list) else "another mode"
    return f"The mode needs {named} to be on first; ask the person whether to switch on both."


async def execute(ctx: OperationContext) -> dict[str, Any]:
    op, params = ctx.operation, ctx.params
    client = cast(SyncRuntime, runtime(ctx.context)).client
    body = {"on": bool(params["on"]), "operation_id": op.id}
    try:
        await client.request("PUT", target(op.task_id, params["mode"]), body)
    except HubRefusal as refusal:
        if refusal.code == "mode_needs":
            raise OperationFailed("mode_needs", message=_needs_message(refusal.detail)) from None
        raise OperationFailed(CODES.get(refusal.status, "upstream_error")) from None
    except HubTransportError:
        raise OperationFailed("hub_unreachable", retryable=True) from None
    return {"mode": params["mode"], "on": bool(params["on"])}


EXECUTORS: dict[str, Callable[..., Any]] = {"task.mode": execute}
