"""`get_usage {workgroup_id?, since, until?, by?, before?, limit? <= 200}` (03-api.md section 8.1, ADR-095 section 3):
the usage of one workgroup the session may read (`workgroup_id`, default the session's; `ReadScope.readable`, ADR-089)
from `GET /v1/device/workgroups/{wg}/usage` - one row per task (`by` `task`, the default) or per session started in
the period, newest first, with requests, tokens, provider cost and charged amount in micro-USD, runtime and operation
counts; session rows add the subagent tree. `total` covers the whole period on the first page only (null with
`before`). Rows are checked and bounded by `analysis_api`. Workgroup level.
"""

from __future__ import annotations

from agent_runner.queries import analysis_api, common
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

ARGUMENTS = frozenset({common.WORKGROUP_ID, "since", "until", "by", "before", "limit"})


async def get_usage(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found = common.arguments(call, ARGUMENTS, frozenset({"since"}))
        since, until, _end = analysis_api.period(found)
        assert since is not None
        by = analysis_api.optional(found, "by", analysis_api.one_of(analysis_api.BY)) or "task"
        before = analysis_api.cursor(found)
        limit = common.integer(found, "limit", 1, analysis_api.MAX_LIMIT, analysis_api.DEFAULT_LIMIT)
        scope = common.scope_of(call)
        scope.require_workgroup()
        wg_id = await common.workgroup(call, scope, found)
        result = await analysis_api.usage(
            call.context, wg_id, since=since, until=until, by=by, before=before, limit=limit
        )
        count = len(result["rows"])
        return common.ok(result, count, target_kind="workgroup", target_id=wg_id, read_wg_id=wg_id)

    return await common.answered(read)


TOOL = get_usage
