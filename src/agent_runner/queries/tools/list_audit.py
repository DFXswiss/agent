"""`list_audit {workgroup_id?, since?, until?, action?, task_id?, session_id?, group_by?, before?, limit? <= 200}`
(03-api.md section 8.1, ADR-095 section 3): the audit rows of one workgroup the session may read (`workgroup_id`,
default the session's; `ReadScope.readable`, ADR-089) from `GET /v1/device/workgroups/{wg}/audit`, newest first, one
page with the hub's cursor; `action` is an action or a dotted group (`operation`). With `group_by` `action` the counts
per action and result of the filtered rows instead (largest first, at most 200 groups, `truncated`). Rows of another
organization or workgroup are dropped and a row's `detail` reaches the model only as flat tokens (`analysis_api`).
Audit rows without a workgroup are not read by sessions. Workgroup level.
"""

from __future__ import annotations

from agent_runner.queries import analysis_api, common
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

ARGUMENTS = frozenset(
    {common.WORKGROUP_ID, "since", "until", "action", "task_id", "session_id", "group_by", "before", "limit"}
)
UUID = analysis_api.pattern(analysis_api.UUID_RE)


async def list_audit(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found = common.arguments(call, ARGUMENTS)
        since, until, _end = analysis_api.period(found)
        query = {
            "since": since,
            "until": until,
            "action": analysis_api.optional(found, "action", analysis_api.pattern(analysis_api.ACTION_RE)),
            "task": analysis_api.optional(found, "task_id", UUID),
            "session": analysis_api.optional(found, "session_id", UUID),
            "before": analysis_api.cursor(found),
        }
        grouped = analysis_api.optional(found, "group_by", analysis_api.one_of(frozenset({"action"}))) is not None
        limit = common.integer(found, "limit", 1, analysis_api.MAX_LIMIT, analysis_api.DEFAULT_LIMIT)
        scope = common.scope_of(call)
        scope.require_workgroup()
        wg_id = await common.workgroup(call, scope, found)
        result = await analysis_api.audit(call.context, scope.org_id, wg_id, query=query, grouped=grouped, limit=limit)
        count = len(result["groups"] if grouped else result["rows"])
        return common.ok(result, count, target_kind="workgroup", target_id=wg_id, read_wg_id=wg_id)

    return await common.answered(read)


TOOL = list_audit
