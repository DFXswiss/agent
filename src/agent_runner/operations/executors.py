"""Executors of the operation area (`<area>/executors.py:EXECUTORS`, ADR-027 section 4; 05-runner.md sections 6.5 and
12.1; 03-api.md sections 9 and 10).

`task.set_title`: the device writes a device-authored `task.meta` event `{title}` (workgroup scope, the operation's
task, empty `session` and `ref`; 04-crypto.md section 4.3 allows a device author for exactly this) through the
outbox; the result is `{}`. The title was checked by the formal checks (1-120 characters, single line).

`human.question` / `human.approval` (KOR-39, ADR-076 section 3): nothing to do - the question travels sealed in the
operation's `operation.params` event, the hub derives `needs_decision` from the succeeded operation, and the result is
`{}` at once. Only a main session may ask a person: the hub denies a subagent's request (`rights_denied`, check H4);
should a hub accept one anyway (00 section 3 B2: the hub is not trusted), the executor fails it `rights_denied`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.crypto.ids import uuid7
from agent_runner.operations.extensions import OperationContext, OperationFailed
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


async def ask_person(ctx: OperationContext) -> dict[str, Any]:
    if ctx.session.depth > 0:
        raise OperationFailed("rights_denied", message="Only the main session of a task may ask a person.")
    return {}


EXECUTORS: dict[str, Callable[..., Any]] = {
    "task.set_title": set_title,
    "human.question": ask_person,
    "human.approval": ask_person,
}
