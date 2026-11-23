# KOR-29 – contract changes

ADR-027 section 3 (additive), ADR-059, `docs/platform/spec/13-work-vm.md`.

## Operations `workvm.request` and `workvm.release` (new)

`ops/workvm.request.{params,result}.json`, `ops/workvm.release.{params,result}.json`, their `$defs` in
`ops/objects.json` (`workvm_id`, `workvm_size`, `workvm_scope`, params and results; both results in the `result`
union), `ops/INDEX.json` (no dedupe fields: both are idempotent by nature), the two branches of
`events/objects.json#/$defs/body_operation_params`. New operation error code `workvm_unavailable`
(`enums/operation-error-codes.json`, `common.json#/$defs/operation_error_code`): the device offers no work VM (no
work image, or its internet path through Cloudflare is not ready). An older hub refuses both types with
`unknown_type`; the hub is therefore rolled out before the runner.

## Work VM tools (new)

`mcp/tools/workvm_{exec,output,kill,status,sync}.{input,output}.json` with their `$defs` in `mcp/objects.json`, rows in
`mcp/INDEX.json` (`read_audit: null`, `via: "workvm.call"`), the `tools/call` alternatives and the `tools/list` names,
the `tool_result` union and the tool list of `platform.refused`. The job object is inlined in the three job outputs
(the contract harness resolves one `$ref` hop per wrapper). `env` is a list of `{name, value}` objects because the
guest's validator implements neither `propertyNames` nor `maxProperties`.

## Journal kind `workvm.call` (new)

`guest/journal/workvm.call.json` (`{pending_id, tool, arguments}`, one alternative per work VM tool), its row in
`guest/journal/INDEX.json` after `platform.query` and its branch in `guest/journal/record.json`. The runner answers
it like a `platform.query` but records no `platform.read` event and no read audit (the calls are visible as
`session.tool` events of the transcript); an older runner leaves the record unanswered (the guest's timeout).

## Addendum 2: tool `workvm_browse`, events `workvm.run` and `workvm.state` (new)

`mcp/tools/workvm_browse.{input,output}.json` (the browser of the work VM: read, screenshot, click, fill) with its
rows like the other work VM tools. Two device-authored, workgroup-scoped session event kinds (right
`device_assigned_wg_session`, header `task` and `session` set, like `session.tool`): `workvm.run` (every job of a work
VM: command, state, exit code, times, output tail – KONZEPT idea 27) and `workvm.state` (created / deleted with reason
and runtime – KONZEPT ideas 44, 46): rows in `enums/event-kinds.json` and `events/kinds.json`, bodies
`events/workvm.{run,state}.json`, both kinds in the `header_device` kind list. An older hub refuses these events
(`kind_not_allowed`), so the hub is rolled out before a runner that writes them.

## Stage b: work VMs in the monitoring (additive)

`common.json#/$defs/workvm_id` (`wvm-<8 hex>`). `outbox/objects.json#/$defs/metric_sample`: entity kind `workvm` with
a work VM ID and the optional property `session_id` (set on `workvm` samples only and required there by the hub: the
session the work VM belongs to). `api/monitoring-overview.response.json`: optional list `workvms` (work VM, device,
session, task, workgroup, state of the newest sample, `latest`, `stale`); an older hub omits it.
`api/monitoring-series.request.json` and `ws/sse/objects.json#/$defs/data_metrics`: entity kind `workvm`. An older hub
refuses a `metrics` item with a `workvm` sample (`request_invalid`), so the hub is rolled out before the runner; an
older web ignores the new list and the new kind in live frames.
