# KOR-53 – process analysis (ADR-097)

Additive changes; an older client, runner or guest never sends the new fields and refuses none of the old ones.

- `api/tasks-create.request.json`: optional `analysis {scope: org|wg, since?, until?}` (ADR-097 section 1).
- `api/objects.json`: `$defs/analysis`; `task_row.analysis` (optional, null for every other task).
- `api/INDEX.json`, `api/device-workgroup-{usage,audit,operations,metrics}.{request,response}.json`: the device reads
  of one workgroup (right `device:wg`, read resources `device.workgroup.<name>`; ADR-097 section 4).
- `audit/read.device.workgroup.{usage,audit,operations,metrics}.json`, `audit/read.session.{get_usage,list_audit,
  list_workgroup_operations,get_metrics}.json`; `enums/audit-actions.json`, `enums/outbox-audit-actions.json`.
- `audit/task.create.json`: optional `analysis` (`org`|`wg`).
- `api/session-orchestrator-get.response.json`, `api/session-orchestrator-request.response.json`: `reason`
  `analysis` (a session of an analysis task never becomes the main orchestrator).
- `common.json` error detail `reason`: `analysis` (`orchestrator_unavailable` for a session of an analysis task;
  `forbidden` when the profile of an analysis task would be widened or cleared).
- `mcp/objects.json`, `mcp/tools/{get_usage,list_audit,list_workgroup_operations,get_metrics}.{input,output}.json`,
  `mcp/INDEX.json`, `mcp/tools-call.params.json`, `mcp/tools-list.result.json` (16 tools),
  `events/objects.json` (`platform.read`), `guest/journal/record.json` (`platform.query`, `platform.refused`),
  `outbox/objects.json` (read audit details): the four read tools (ADR-097 section 3).
- `mcp/objects.json` `$defs/tool_result`: the four outputs (the guest checks a pending answer against it).
- `ops/classes.json` `tools`: the four tools are of class `read`.
- `tasks/analysis.json`: the fixed prompt and title of an analysis task (web and agent tool).
