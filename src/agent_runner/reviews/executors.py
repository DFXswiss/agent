"""Executors of this area (`<area>/executors.py:EXECUTORS`; ADR-093 section 4).

- `review.verdict`: records the verdict; the result names it (`verdict_id` = the operation ID) with the name of the
  session's effective rights profile at this moment (`profile`, null without), which the merge criteria compare.
- `security.report`: records the structured scan result (`report_id` = the operation ID).

Both are platform objects: the operation row (hub) and its sealed result (`operation.result`), readable through
`get_operation`, `list_operations`, `github.list_reviews` (verdicts) and the web and `agent-platform`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.operations.extensions import OperationContext
from agent_runner.orgs.db import session as store_session
from agent_runner.profiles import state


async def verdict(ctx: OperationContext) -> dict[str, Any]:
    async with store_session(ctx.context.pool) as conn:
        profile = await state.effective(conn, ctx.session.id, ctx.session.parent_id)
    params = ctx.params
    return {
        "verdict_id": ctx.operation.id,
        "repo": params["repo"],
        "number": params["number"],
        "head_sha": str(params["head_sha"]).lower(),
        "outcome": params["outcome"],
        "profile": None if profile is None else profile.name,
    }


async def report(ctx: OperationContext) -> dict[str, Any]:
    params = ctx.params
    return {
        "report_id": ctx.operation.id,
        "repo": params["repo"],
        "ref": params["ref"],
        "tool": params["tool"],
        "passed": params["passed"],
        "findings": dict(params["findings"]),
    }


EXECUTORS: dict[str, Callable[..., Any]] = {"review.verdict": verdict, "security.report": report}
