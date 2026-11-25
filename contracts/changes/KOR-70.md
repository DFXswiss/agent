# KOR-70 – contract changes

ADR-027 section 3 (additive); design: ADR-093 (workgroup VMs move to the current golden by themselves).

- `common.json`: `vm_upgrade` (`golden_version`, `golden_built_at`, `state` `waiting`/`switching`, `holds`, `sessions`)
  and `vm_upgrade_hold` (`reason` `session_active`, `subagent`, `checkpoint_due`, `checkpoint_missing`,
  `workspace_incomplete`, `capacity`, `switch_failed`; optional `session_id`; `since`); `context_reason` gains
  `golden_switch`.
- `outbox/objects.json` `body_vm_state`: optional `upgrade` (`vm_upgrade` or `null`; absent = unchanged) and
  `replaced_by` (`vm_id`, never `null`). A runner sends them only to a hub whose desired state names `vm_switch`.
- `api/objects.json` `vm_load`: optional `golden_version` and `upgrade` (`GET /v1/workgroups/{wg}`).
- `api/device-state.response.json`: optional `vm_switch` (`[vm_id]`, at most 1000).
- New endpoint (`api/INDEX.json`, owner KOR-70, `tests/platform/contracts/spec/endpoints-kor70.json`):
  `POST /v1/workgroups/{wg}/vms/{vm}/switch` (`api/workgroup-vm-switch.request.json`, `.response.json`).
- `audit/session.context_restored.json` (and its copy in `outbox/objects.json`): optional `moved` (`golden`).
- `enums/audit-actions.json` and `audit/vm.switch_requested.json`: `vm.switch_requested {vm_id, device_id}` (group
  `task`).
