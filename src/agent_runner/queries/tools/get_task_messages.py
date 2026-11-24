"""`get_task_messages {task_id, after?, limit? <= 200}` (03-api.md section 8.1, ADR-024): the chat of a task of the
session's workgroup - prompts and outputs in the hub's scope order (`seq`), decrypted from the kept verified events
(`projection.messages`), from `after` (exclusive), with `next_after` while more follow. Prompts this device withdrew
are left out (ADR-083 section 3). Workgroup level; a task that
is neither known from a kept event nor from the hub in this workgroup is `not_found`.
"""

from __future__ import annotations

from agent_runner.queries import common, projection
from agent_runner.queries.rights import read_scope
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

DEFAULT_LIMIT, MAX_LIMIT = 100, 200
MAX_SEQ = 2**53 - 1


async def get_task_messages(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"task_id", "after", "limit"}), frozenset({"task_id"}))
        task_id = common.uuid_argument(found_args, "task_id")
        after = common.integer(found_args, "after", 0, MAX_SEQ, 0)
        limit = common.integer(found_args, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
        scope = read_scope(call.context, call.session)
        scope.require_workgroup()
        await common.known_task(call.context, scope, task_id)
        async with common.store(call.context) as (conn, key):
            items, following = await projection.messages(
                conn, key, wg_id=scope.wg_id, task_id=task_id, after=after, limit=limit
            )
        result = {"messages": items, "next_after": following}
        return common.ok(result, len(items), target_kind="task", target_id=task_id)

    return await common.answered(read)


TOOL = get_task_messages
