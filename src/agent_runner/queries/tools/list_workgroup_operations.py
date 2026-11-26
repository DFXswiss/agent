"""`list_workgroup_operations {workgroup_id?, since?, until?, type?, state?, group_by?, before?, limit? <= 200}`
(03-api.md section 8.1, ADR-095 section 3): the operations of every task of one workgroup the session may read
(`workgroup_id`, default the session's; `ReadScope.readable`, ADR-089) from
`GET /v1/device/workgroups/{wg}/operations`, newest first, one page with the hub's cursor. With `group_by` `type` the
counts per type, state and error code instead (largest first, at most 200 groups, `truncated`). Rows are checked and
bounded by `analysis_api`. `list_operations` stays the read of the session's own task on this device. Workgroup level.
"""

from __future__ import annotations

from agent_runner.queries import analysis_api, common, device_api
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

ARGUMENTS = frozenset({common.WORKGROUP_ID, "since", "until", "type", "state", "group_by", "before", "limit"})


async def list_workgroup_operations(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found = common.arguments(call, ARGUMENTS)
        since, until, _end = analysis_api.period(found)
        query = {
            "since": since,
            "until": until,
            "type": analysis_api.optional(found, "type", analysis_api.pattern(device_api.OPERATION_TYPE_RE)),
            "state": analysis_api.optional(found, "state", analysis_api.one_of(device_api.OPERATION_STATES)),
            "before": analysis_api.cursor(found),
        }
        grouped = analysis_api.optional(found, "group_by", analysis_api.one_of(frozenset({"type"}))) is not None
        limit = common.integer(found, "limit", 1, analysis_api.MAX_LIMIT, analysis_api.DEFAULT_LIMIT)
        scope = common.scope_of(call)
        scope.require_workgroup()
        wg_id = await common.workgroup(call, scope, found)
        result = await analysis_api.operations(call.context, wg_id, query=query, grouped=grouped, limit=limit)
        count = len(result["groups"] if grouped else result["operations"])
        return common.ok(result, count, target_kind="workgroup", target_id=wg_id, read_wg_id=wg_id)

    return await common.answered(read)


TOOL = list_workgroup_operations
