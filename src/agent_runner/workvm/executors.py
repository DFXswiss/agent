"""Executors of `workvm.request`, `workvm.release`, `test.run`, `browser.open` and `browser.close`
(`<area>/executors.py:EXECUTORS`, ADR-027 section 4, ADR-041; docs/platform/spec/13-work-vm.md sections 2, 12 and 13,
ADR-080, ADR-085).

They run after the hub accepted the operation (the request is audited there). Without work VMs on this device the
operation fails `workvm_unavailable`. A new VM keeps the operation's acting user as the person who triggered it
(ADR-083 section 2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.workvm.manager import NOT_ENABLED
from agent_runner.workvm.service import browsers_for, testruns_for, workvms_for


async def request_workvm(ctx: OperationContext) -> dict[str, Any]:
    workvms = workvms_for(ctx.context)
    if workvms is None:
        raise OperationFailed("workvm_unavailable", message=NOT_ENABLED)
    return await workvms.request(ctx.session, ctx.params, triggered_by=ctx.operation.actor_id)


async def release_workvm(ctx: OperationContext) -> dict[str, Any]:
    workvms = workvms_for(ctx.context)
    if workvms is None:
        return {"released": False}
    return await workvms.release(ctx.session, ctx.params)


async def run_tests(ctx: OperationContext) -> dict[str, Any]:
    runs = testruns_for(ctx.context)
    if runs is None:
        raise OperationFailed("workvm_unavailable", message=NOT_ENABLED)
    return await runs.execute(ctx)


async def open_browser(ctx: OperationContext) -> dict[str, Any]:
    browsers = browsers_for(ctx.context)
    if browsers is None:
        raise OperationFailed("workvm_unavailable", message=NOT_ENABLED)
    return await browsers.open(ctx)


async def close_browser(ctx: OperationContext) -> dict[str, Any]:
    browsers = browsers_for(ctx.context)
    if browsers is None:
        return {"closed": False, "browser_id": None, "actions": 0, "reason": None}
    return await browsers.close(ctx)


EXECUTORS: dict[str, Callable[..., Any]] = {
    "workvm.request": request_workvm,
    "workvm.release": release_workvm,
    "test.run": run_tests,
    "browser.open": open_browser,
    "browser.close": close_browser,
}
