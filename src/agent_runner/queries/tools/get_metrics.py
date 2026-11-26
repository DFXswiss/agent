"""`get_metrics {workgroup_id?, entity_kind, entity_id, metric, since, until?, step}` (03-api.md section 8.1, ADR-095
section 3): the monitoring series of a session, workgroup VM or work VM of one workgroup the session may read
(`workgroup_id`, default the session's; `ReadScope.readable`, ADR-089) from `GET /v1/device/workgroups/{wg}/metrics`
(the rules of `GET /v1/monitoring/series`), at most 1000 points `[bucket start, value]`. An entity that is not of the
workgroup is `not_found`. Points are checked and bounded by `analysis_api`. Workgroup level.
"""

from __future__ import annotations

from agent_runner.queries import analysis_api, common
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

ARGUMENTS = frozenset({common.WORKGROUP_ID, "entity_kind", "entity_id", "metric", "since", "until", "step"})
REQUIRED = frozenset({"entity_kind", "entity_id", "metric", "since", "step"})


async def get_metrics(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found = common.arguments(call, ARGUMENTS, REQUIRED)
        since, until, end = analysis_api.period(found)
        assert since is not None
        kind = analysis_api.optional(found, "entity_kind", analysis_api.one_of(frozenset(analysis_api.ENTITY_ID_RES)))
        entity_id = analysis_api.optional(found, "entity_id", analysis_api.pattern(analysis_api.ENTITY_ID_RES[kind]))
        metric = analysis_api.optional(found, "metric", analysis_api.one_of(analysis_api.METRICS))
        step = common.integer(found, "step", analysis_api.MIN_STEP, analysis_api.MAX_STEP, 0)
        if analysis_api.points(since, end, step) > analysis_api.MAX_POINTS:
            raise common.PARAMS_INVALID
        scope = common.scope_of(call)
        scope.require_workgroup()
        wg_id = await common.workgroup(call, scope, found)
        query = {
            "entity_kind": kind,
            "entity_id": entity_id,
            "metric": metric,
            "since": since,
            "until": until,
            "step": str(step),
        }
        result = await analysis_api.metrics(call.context, wg_id, query=query)
        count = len(result["points"])
        return common.ok(result, count, target_kind="workgroup", target_id=wg_id, read_wg_id=wg_id)

    return await common.answered(read)


TOOL = get_metrics
