"""`get_operation {operation_id, wait_seconds? <= 30}` (03-api.md sections 8.1 and 9): the current outcome of an
operation of the session's task, from the operation pipeline (`Pipeline.wait_answer`, RUN-07): at once when it is
final, else after `wait_seconds` (default 0) with `status:"pending"`. An operation of another task, or one this
device does not know, is `not_found` - also of a task of another readable workgroup (ADR-089 section 3: parameters
and results stay with the session's own task). Workgroup level.
"""

from __future__ import annotations

from agent_runner.operations.pipeline import pipeline_for
from agent_runner.queries import common
from agent_runner.queries.rights import ReadRefused
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

MAX_WAIT_S = 30
NOT_FOUND = ReadRefused("not_found", "There is no such operation in this task.")
NO_PIPELINE = ReadRefused("unavailable", "Operations are not available on this device right now.")


async def get_operation(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"operation_id", "wait_seconds"}), frozenset({"operation_id"}))
        operation_id = common.uuid_argument(found_args, "operation_id")
        wait = common.integer(found_args, "wait_seconds", 0, MAX_WAIT_S, 0)
        scope = common.scope_of(call)
        scope.require_workgroup()
        pipeline = pipeline_for(call.context)
        if pipeline is None:
            raise NO_PIPELINE
        op = await pipeline.load(operation_id)
        if op is None or op.task_id != call.session.task_id or op.wg_id != scope.wg_id:
            raise NOT_FOUND
        answer = await pipeline.wait_answer(operation_id, wait)
        assert answer is not None
        return common.ok(answer, 1, target_kind="operation", target_id=operation_id, read_wg_id=scope.wg_id)

    return await common.answered(read)


TOOL = get_operation
