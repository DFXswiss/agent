"""`get_task_messages {task_id, after?, limit? <= 200}` (03-api.md section 8.1, ADR-089): the chat of a task of a
workgroup the session may read (the hub's task row names it) - prompts and outputs in the hub's scope order (`seq`),
decrypted from the kept verified events sealed in that workgroup's scope (`projection.messages`), from `after`
(exclusive), with `next_after` while more follow. Prompts this device withdrew are left out (ADR-083 section 3).
Workgroup level; a task the hub does not name in a readable workgroup is `not_found`.
"""

from __future__ import annotations

from agent_runner.queries import common, projection
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

DEFAULT_LIMIT, MAX_LIMIT = 100, 200
MAX_SEQ = 2**53 - 1


async def get_task_messages(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"task_id", "after", "limit"}), frozenset({"task_id"}))
        task_id = common.uuid_argument(found_args, "task_id")
        after = common.integer(found_args, "after", 0, MAX_SEQ, 0)
        limit = common.integer(found_args, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
        scope = common.scope_of(call)
        scope.require_workgroup()
        wg_id = (await common.known_task(call.context, scope, task_id)).task.wg_id
        async with common.store(call.context) as (conn, key):
            items, following = await projection.messages(
                conn, key, wg_id=wg_id, task_id=task_id, after=after, limit=limit
            )
        result = {"messages": items, "next_after": following}
        return common.ok(result, len(items), target_kind="task", target_id=task_id, read_wg_id=wg_id)

    return await common.answered(read)


TOOL = get_task_messages
