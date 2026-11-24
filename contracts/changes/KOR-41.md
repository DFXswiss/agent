# KOR-41 – contract changes

ADR-027 section 3 (additive); design: ADR-075 (resource choice).

- `common.json`: membership-log bodies `log_body_device_assign_org` (`device`, optional `platform_seq`) and
  `log_body_device_unassign_org` (`device`) in `log_entry_body`; schemas `log/membership/device_assign_org.json` and
  `log/membership/device_unassign_org.json` with fixtures. Older readers do not know the types (`log_type`); the hub
  refuses them unless every device of the org announced `log.org_assign` on its live channel (ADR-075 §5, the
  mechanism of ADR-060).
- `common.json` `capacity`: optional counters `cpus`, `memory_total_mib`, `disk_free_mib`, `vms_running`,
  `sessions_active`, `work_vms_running` and the settings `sessions_per_vm`, `vms_max`. The five original counters stay required; enrollment bodies keep only them.
- `common.json` `task_waiting_reason` (new) and `api/objects.json` `task_row.state_reason`, `enums/task-states.json`:
  the waiting reasons `no_device`, `platform_access_required`, `wg_vm_limit`, `placement_full`,
  `placement_offline`, `keys_pending` (only as `state_reason` of a `scheduling` task; the session failure reasons are
  unchanged) and `scheduling_categories` (reason → `waiting_for_capacity` / `waiting_for_device`, for the situation
  view).
- `enums/error-codes.json`: `placement_forbidden` (403), `placement_invalid` (409); the compatibility refusal is the
  existing `peer_outdated` of ADR-060. `no_device`
  stays listed but is no longer answered by `POST /v1/tasks`.
- `api/tasks-create.request.json`: optional `placement {device_id, vm_id?}`.
- `api/objects.json`: `placement`, `vm_load`; `task_row.placement` (optional); `device_ref.assigned_via` and
  `device_ref.vms` (optional).
- `api/workgroup-get.response.json`: optional `placement`. `api/org-devices.response.json`: optional `assigned_org`
  and `vms`.
- `api/device-state.response.json`: optional `vm_id_pref` and `vm_new_allowed` per session.
- `outbox/objects.json` `body_session_transition`: optional `vm_id`.
- New endpoints (`api/INDEX.json`, owner KOR-41, `tests/platform/contracts/spec/endpoints-kor41.json`):
  `GET`/`PUT /v1/workgroups/{wg}/placement`, `PUT /v1/tasks/{task}/placement`, `GET /v1/platform/waiting`.
- `enums/audit-actions.json` and `audit/`: `log.device_assign_org`, `log.device_unassign_org`, `task.placement`,
  `wg.placement`, `read.workgroup.placement`, `read.platform.waiting`.

Compatibility: every change is optional for older readers except the two log types (guarded as above) and the
heartbeat counters, which an older hub refuses – the hub is released before the runner.
