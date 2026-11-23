"""Executors of the operation area (`<area>/executors.py:EXECUTORS`, ADR-027 section 4; 05-runner.md sections 6.5 and
12.1; 03-api.md sections 9 and 10).

`task.set_title`: the device writes a device-authored `task.meta` event `{title}` (workgroup scope, the operation's
task, empty `session` and `ref`; 04-crypto.md section 4.3 allows a device author for exactly this) through the
outbox; the result is `{}`. The title was checked by the formal checks (1-120 characters, single line).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.crypto.ids import uuid7
from agent_runner.operations.extensions import OperationContext
from agent_runner.sync.outbox import event_draft


async def set_title(ctx: OperationContext) -> dict[str, Any]:
    op, env = ctx.operation, ctx.env
    draft = event_draft(
        id=uuid7(),
        org=op.org_id,
        scope="wg",
        scope_id=op.wg_id,
        wg=op.wg_id,
        task=op.task_id,
        kind="task.meta",
        ts=env.at(),
        body={"title": ctx.params["title"]},
    )
    async with env.transaction() as conn:
        await env.outbox.enqueue(conn, "event", draft)
    return {}


EXECUTORS: dict[str, Callable[..., Any]] = {"task.set_title": set_title}
