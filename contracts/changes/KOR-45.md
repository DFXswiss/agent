# KOR-45 – contract changes

ADR-027 section 3. Design: ADR-089 (sessions read like their human) and ADR-088 (full subagents and limits as
settings). Every change widens what was valid except where noted ("defect fix": a fixed copy of a limit that is a
setting now).

## Limits as settings (ADR-088 section 2)

- `api/config.response.json` `limits`: `subagent_max_depth`, `subagent_max_running`, `subagent_max_total` are
  `count` (0 = no limit) instead of `positive`; new required `subagent_max_runtime_seconds` (`count`). Producer: the
  hub; consumers: browsers (they only check that `limits` is an object). Fixtures updated to the defaults 5/50/0/0.
- `api/device-state.response.json`: optional section `limits` `{api, subagent_max_depth, subagent_max_running,
  subagent_max_total, subagent_max_runtime_seconds}`; fixture `api/device-state.response/valid/limits.json`.
- `mcp/objects.json` `output_get_context.limits`: each value `count` or null (null: an older hub sent no limits), new
  optional `subagent_max_runtime_seconds`; the maxima 2/3/10 are gone (defect fix). A guest before ADR-088 still
  validates the old range; the runner answers such a guest within it (ADR-088 section 1).
- `depth` of sessions (`mcp/objects.json` `output_get_context.session`, `output_get_task.sessions`,
  `guest/sessions-create.request.json`, `api/objects.json`, `api/device-state.response.json`): `count` instead of
  0-2 (defect fix).
- `ops/rules.json` `subagent_limits`: only `purpose_max_length` and `prompt_max_length` (the counts were a copy of
  the hub's limits; defect fix).
- `enums/attention.json`: reason `limit_reached` of category `blocked` (a subagent limit refused a request).
- `api/session-get.response.json` `children`: up to 1000 IDs (was 10, the former total limit per task).

## Reading like the human (ADR-089)

- `mcp/objects.json`: optional `workgroup_id` (uuid) in the inputs of `list_tasks`, `get_whiteboard` (level
  `workgroup` only) and `list_members` (scope `workgroup` only); fixtures under `mcp/tools/<tool>.input/`. An older
  guest refuses the new argument with its own schema check (`params_invalid`); results keep their shape
  (`list_workgroups.my_access` is now the session's access).
- `audit/read.session.<tool>.json` (all eleven) and the outbox audit copy (`outbox/objects.json`): optional
  `read_wg_id` and `triggered_by` (uuid or null) beside the required `result_count`. Producer: the runner; consumer:
  the hub (`ReadCount`, `audit/detail_schemas.py`). Rollout: hub before runner (an older hub refuses the fields).

## Full subagents (ADR-088 section 1)

- `ops/INDEX.json`: `subagent.instruct` and `subagent.stop` (both `dedupe: false`); `ops/subagent.{instruct,stop}.
  {params,result}.json`; `ops/objects.json` `params_subagent_instruct` `{session_id, prompt 1-32768}` → result
  `{session_id, instruction_id}`, `params_subagent_stop` `{session_id}` → result `{session_id, state:
  stopping|stopped|failed}`, both in the result union; `events/objects.json` two `operation.params` variants. An older
  hub answers the new types `unknown_type` (rollout: hub first).
- `mcp/objects.json` `output_get_subagent_result`: a branch for `idle`/`suspended` with `final_text`, `finished_at` and
  `instruction_id`; optional `instruction_id` on the `stopped` and running branches. The former branches stay valid;
  the runner answers an older guest only with them.
- Fixtures: ops params/results, `operation.params` variants 19 and 20, `get_subagent_result` outputs `idle`,
  `suspended`, `working-instruction` and two invalid ones.

## Reporting never cuts (ADR-088 section 3)

- `guest/health.response.json`, `guest/objects.json` (`metrics.sessions`), `guest/journal/record.json`
  (`body_metrics.sessions`): no `maxItems: 64`. An older runner refuses a guest metrics answer of more than 64
  sessions (rollout: runner before golden).
