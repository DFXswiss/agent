# KOR-29 (implicit start) – contract changes

ADR-027 section 3 (additive). Producer: the runner (`agent_runner.workvm.manager`); consumer: the guest's check of
tool results (`agent_guest.mcp_tools._forward.outcome`); 13-work-vm.md sections 3 and 7.

A cold start of a work VM can take minutes, longer than the guest waits for a tool call (at most 33 s). Until now
`workvm_exec` and `workvm_browse` started a VM implicitly and then ran into the guest's `timeout`, and
`workvm_status` answered `workvm: null` during the start. The runner now joins every further call of the same scope to
the start in flight and answers within the guest's budget.

## `mcp/objects.json`

- New `$defs/output_workvm_starting` `{workvm_id, state: "starting", started_at, queue_position, message}`: the answer
  of `workvm_exec` and `workvm_browse` while the implicit start goes on; nothing ran in the VM.
- New `$defs/workvm_queue_position`: integer 1-100000 (1: at the head of the device's capacity queue, where the VM is
  cloned and booted) or null (not waiting in the queue: formal checks, warm pool).
- `output_workvm_status`: the `workvm` object gains the optional `started_at` (the moment the implicit start began)
  and `queue_position`; both are present while an implicit start of the session's scope is in flight.
- `tool_result` accepts `output_workvm_starting`.

## `mcp/tools/workvm_exec.output.json`, `mcp/tools/workvm_browse.output.json`

`oneOf` the previous result and `output_workvm_starting`.

Version 0.8.0: the contracts the guest bundles change. The runner gives the `starting` answer and the two status fields
only to guests from 0.8.0 (`agent_runner.subagents.guest_features.START_ANSWER`); an older guest gets the same
sentence as the tool error `unavailable` and the status without `started_at` and `queue_position`.

Fixtures: `valid/starting.json` and invalid starting shapes under `fixtures/mcp/tools/workvm_exec.output/`,
`workvm_browse.output/` and `workvm_status.output/`.
