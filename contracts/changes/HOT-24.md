# HOT-24 – contract changes

ADR-027 section 3 (additive). Design: ADR-093 addendum HOT-24 (no VM stays on an old golden forever because a session
can never be checkpointed).

- `common.json` `vm_upgrade_hold`: new `reason` `checkpoint_impossible` (a paused session that worked can never be
  checkpointed on this VM; only a switch by hand moves the VM) and the optional `checkpoint_at` (`time`, only with
  that reason): when the checkpoint was taken that the session would continue from after a switch by hand, absent when
  it would start fresh.
- `api/device-state.response.json`: optional `vm_upgrade_version` (integer, 1-1000). The hub sends `2`; a runner sends
  `checkpoint_impossible` and `checkpoint_at` only to a hub that names 2 or later and reports `checkpoint_missing`
  without `checkpoint_at` to an older one (which refuses unknown values).
- `api/device-state.response.json`: optional `vm_switch_at` (object `vm_id` -> `time`, at most 1000): the time of the
  newest request of each VM of `vm_switch`. A request by hand moves a session held `checkpoint_impossible` only when the
  device had reported that hold before this time; a repeated `POST /v1/workgroups/{wg}/vms/{vm}/switch` now sets
  `requested_at` to its own time (before: kept when already set), so it confirms the holds shown at that moment.
- `common.json` `vm_upgrade.holds`: `maxItems` 16 -> 64. A runner sends more than 16 only to a hub of
  `vm_upgrade_version` 2 (a request by hand covers only the holds that were reported, so all of them must fit).
- Fixtures: `outbox/item/valid/vm_state-upgrade-impossible.json`, `outbox/item/invalid/vm_state-checkpoint-at-null.json`,
  `outbox/item/invalid/vm_state-too-many-holds.json` (now 65 holds), `api/device-state.response/valid/vm-switch-at.json`,
  `api/device-state.response/invalid/vm-switch-at-not-a-vm.json` and `vm-upgrade-version-zero.json`.
