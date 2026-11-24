# KOR-41 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent`
(INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent`
repository, then the note of the former `agent-core` repository.

## KOR-41 – Resource choice: runner and agent tool

Decisions taken while implementing ADR-075 in the agent repository (runner, `agent-platform`). The ADR holds the
design; this file names the points it left open.

1. **Crypto copy.** `agent_runner.crypto` was copied again with `copy_tool.py` from the agent-core commit that adds
   `device_assign_org`/`device_unassign_org` (only `log/{entries,state,verify,vectors}.py` and the log vector test
   changed in the reference). The copied `test_entries.py` gets a recorded adaptation: the two organization entries
   live in the `orgwide` vector log, not in `main`, so the reference assertion "`main` uses every log type" fails in
   agent-core as well. Once agent-core changes its test the adaptation is dropped. At merge time the copy should be
   re-run against the merged agent-core commit (the recorded commit is the package branch's).
2. **Runner uses of assignments.** Every runner use goes through `OrgState.assigned()`/`device_wgs()` (book,
   assignment teardown, rights of queries, member listing, catch-up planning through the copied `crypto.log.catchup`
   and `wraps`), so an organization assignment counts everywhere without further changes; catch-up wrapping for an
   organization device works through the copied W2 rules. The enrollment's platform-log detection also treats
   `device_assign_org` with `platform_seq` as a platform assignment.
3. **Several VMs per workgroup.** Runner migration 0018 drops `vm_wg_id_live` and adds non-unique indexes on `runner.vm
   (wg_id)` (live rows) and `runner.session (vm_id)`. `records.for_workgroup` is replaced by `of_workgroup` (all live
   VMs, smallest ID first); `delete_workgroup` deletes every VM of the workgroup and returns the deleted rows (the
   teardown line gains `vms=<n>`).
4. **`ensure_vm` signature.** `ensure_vm(org_id, wg_id, *, session_id=None, prefer=(), new_allowed=True,
   only_preferred=False)`. Without `session_id` nothing is booked (operator and test callers, the old behaviour);
   with it, `runner.session.vm_id` is written under the workgroup lock before the lock is released. The lock is held
   for the whole choice including a restart or clone, as before, so a booking is never lost.
5. **Preferred VMs** (`vm_id_pref`, the parent's VM, the `resume_from` session's VM) are taken when they are a live
   VM of the workgroup in state `running` (used) or `stopped`/`stopping` (restarted). A preferred stopped VM is
   restarted **without** the golden replacement rule: the user chose it, or the work of the continued session lives
   there. A preferred VM in another state (`assigning`), of another workgroup or unknown is skipped and logged
   (`vm.preference_skipped`).
6. **Golden replacement and `vm_new_allowed`.** When step 4 finds a stopped VM with room whose golden is outdated,
   it is deleted and replaced by a new VM even when the hub sent `vm_new_allowed: false`: the replacement does not
   add a VM. Without such a VM, `vm_new_allowed: false` ends the choice with `capacity_exhausted` (`vm_new_allowed`).
7. **Occupying sessions** are counted from the runner's own `runner.session` rows (`ACTIVE_SESSION_STATES`),
   children included (they run in the same guest). A session's own row counts from the moment it is booked.
8. **Resume of a suspended session** (`prompts/resume.py`) asks only for the session's own VM
   (`only_preferred=True`); a session without a recorded VM fails `vm_start_failed` without asking the VM manager.
   Another VM of the workgroup is never taken.
9. **`vm_id` in transitions.** `session_transition` carries `vm_id` whenever the local row names a VM. The row gets
   it when the VM is chosen, after the `vm_assigning` transition, so `vm_ready` is the first transition with it and
   every later one (including `failed` and `stopped`) keeps it.
10. **Desired state.** `resume_from`, `vm_id_pref` (pattern `vm-<8 hex>` or null) and `vm_new_allowed` (boolean,
    default true when absent) are parsed and checked; a malformed value fails the step like every other field.
11. **Capacity report.** `VmManager.report()` adds `cpus` (`os.cpu_count()`), `memory_total_mib` (physical RAM),
    `disk_free_mib` (free space of the volume of `$LIMA_HOME`), `vms_running` (workgroup VMs in state `running`),
    `sessions_active`, `work_vms_running`, `sessions_per_vm` and `vms_max`; the features `log.org_assign` and `vm.choice`
    go in the `Agent-Features` header of the live channel (the mechanism of ADR-060).
    `cpus` and `disk_free_mib` are left out when they cannot be read. The enrollment body keeps the five base
    counters; its `vm_slots_total` honours `MAX_VMS` like the heartbeat.
12. **Extension point `<area>/capacity.py:USAGE`** (`async USAGE(context) -> vm.capacity.Usage(vms, memory_mib)`),
    discovered by `registry.capacity_usages()` like the other points and listed in the registry's table (no row in
    the ADR-027 amendment table is needed). The VM area's own `vm/capacity.py` provides one that holds nothing (its
    VMs are counted from `runner.vm`). The sum of all providers holds its memory and `max(vms, ceil(memory /
    VM_MEMORY_MIB))` slots of the budget and is reported as `work_vms_running`; a provider that returns another type
    is `registry_invalid`.
13. **Settings.** `AGENT_RUNNER_SESSIONS_PER_VM` (default 16) and `AGENT_RUNNER_MAX_VMS` (default 0), both 0 to
    1 000 000, 0 meaning no such limit; listed in 05 §1.3 and `CUTOVER.md`.
14. **Hub error codes.** `placement_forbidden` and `placement_invalid` are added to the runner's
    copy of the hub codes (`sync/codes.py`), kept equal to the contracts by its test.

### Agent tool (`agent-platform`)

15. `build_batch` computes R the way the hub's `check_batch` does and appends one `rotate` per element (sorted) after
    the trigger, with fresh keys wrapped for every remaining reader. The existing commands never produce an R, so
    their batches are unchanged; `device unassign --org` is the first user.
16. `device assign --org` wraps only the keys the admin holds (`OK`, its `WK`s, its own `CK`, mode `held_only`) and
    returns `keys_pending`, the scopes the device reads now but got no wrap for (catch-up by other holders,
    ADR-075 §5). `device assign --wg` refuses a device that is already assigned through the organization.
17. Batches fetch the verified platform log when the org log contains platform devices, so decertified devices get
    no wraps of new epochs.
18. The parity walker accepts option alternatives (`--wg|--org`); `task-create` is `done` with `--device`/`--vm`.
19. `org show` lists a workgroup's devices through `state.assigned` and adds `org_devices`; `device list` shows the
    hub's view next to the verified log's (`assigned_org`, `assigned_wgs`) – only the verified log decides who gets
    keys.
20. `diagnose` reads the task's `state_reason` first (the waiting reasons of ADR-075 §7 each with a fixed `next`
    step), then the last transition.

## KOR-41 – decisions (hub, contracts, specification, web)

Design: [ADR-075](../adr/075-resource-choice.md). Runner and agent tool: the decisions file of the agent repository.

### Hub, contracts, specification

1. **Hub chooses the device, the runner the VM.** Each with a documented, deterministic order; the hub only knows
   the VMs from `hub.vm` and the sessions from `hub.session.vm_id` (named by the runner from `vm_ready` on,
   `session_transition.vm_id`).
2. **Organization assignment as signed log entries** (`device_assign_org`, `device_unassign_org`), not as a hub flag
   (a hub flag would let a hub make an admin's client give keys to a device). The projection writes the effective
   assignments into `wg_device`, so scheduler, rights and device lists keep reading one table; `org_device` is for
   display.
3. **Waiting reasons are a separate enumeration** (`task_waiting_reason`), not new session failure reasons: a runner
   can never report them as a failure.
4. **The key check applies only to devices assigned through the organization** (explicit assignments carry the wrap
   of the workgroup key in their own batch, and many existing tests and deployments rely on that).
5. **Refusal limit**: the former fixed `MAX_REFUSALS` that failed the task became `AGENT_HUB_CAPACITY_REFUSALS`; at the
   limit the attempt waits and is held back by `AGENT_HUB_REFUSAL_HOLD_SECONDS` (`session.not_before`). Amends ADR-049.
6. **Additive outputs only when set**: `task.placement`, the device-state fields `vm_id_pref` and
   `vm_new_allowed: false` appear only when they carry information, so older clients and existing tests see the same
   bodies as before.
7. **Compatibility gate** `peer_outdated` (shared with ADR-060 after the unification of the repositories): checked
   for batches that add an organization assignment or a device to an org that uses one; the feature `log.org_assign`
   comes from the live channel's `Agent-Features` header (`device.features`), not from the capacity report.
8. **Who may choose** device and VM: everyone with `wg:write` (E-11), the user's agent included (E-04); the device must
   be a candidate of the workgroup (403 `placement_forbidden`).
9. **E2E number 48**: E2E-41 is taken by KOR-37, E2E-45 to 47 by other packages; 48 avoids a collision.
10. **Defaults**: `AGENT_RUNNER_SESSIONS_PER_VM` 16 (8 GiB VM, about 0.5 GiB per Claude Code session),
    `AGENT_RUNNER_MAX_VMS` 0 (memory decides), `AGENT_HUB_VMS_PER_WG` 100.
11. **Numbers after the rebase**: ADR-075, hub migration 0020, runner migration 0018 and E2E-57 on the united tree
    (058–064, hub 0011–0016, runner 0012–0014 and E2E-46/47 went to KOR-34, KOR-29, KOR-37, INF-06, KOR-31, KOR-40
    and others meanwhile). With KOR-31 the scheduler also skips platform devices of an org without credit
    (`credit_exhausted`, category `waiting_for_credit`).
12. **Independent review, handled:** H1 (a device joining by its own `accept` is exempt from the `org_assign`
    check), M1 (removals and owed rotations are never blocked: the check runs only when an organization assignment or a
    device other than by `accept` is added), M2 (a device assigned only through the organization also needs the wraps
    of the current `OK` and of the task owner's current `CK`), L3 (`keys_pending` for a chosen device without keys),
    L5 (a stored wrap for a device emits `wraps.stored`; the scheduler re-runs), L6 (other VM states documented in
    ADR-075 §3), L7 (`capacity.py:USAGE` added to ADR-027's table), M3 (E2E-57 asserts the deterministic candidate).
    Kept as they are, with the reason: L1/L2 (the golden replacement and extra stopped VMs of a workgroup follow the
    existing rules of ADR-009 per VM; a stopped VM gives its RAM back and its disk is the existing VM disk budget –
    reclaiming idle extra VMs is a later tuning), L4 (an unknown `vm_id` names a VM the hub has not seen yet; the
    following `vm_state` report creates the row and the scheduler counts the session by `vm_id` from then on), L8 (the
    runner copy is regenerated from the final agent-core commit at merge time).
13. **The work VMs of KOR-29 report through the discovered point** `workvm/capacity.py:USAGE` (a new file in that
    area, adding nothing to its code): the package text requires that work VMs count against the capacity.

### Web

1. **Log core.** `device_assign_org` / `device_unassign_org` follow the hub reference exactly: `OrgState.orgAssignments`
   (`device -> seq`), `orgAssigned(device)`, `assigned`/`deviceWgs` include every workgroup of the org while the device
   is assigned to it, `remove` clears both, `device_assign` is refused while org-assigned, `device_unassign` needs an
   explicit assignment, the platform-device checks of `device_assign` are shared (`assignable`), the projection adds
   `org_assigned_seq` only when present. Readers, R and catch-up follow from `assigned`; the `wg_create` batch wraps
   `WK_1` for the organization's devices without extra code (they are readers of the new epoch). The vector suite
   checks the `orgwide` section (heads, projections, `device_wgs` per state, org device reads a later workgroup, the
   explicit assignment stays) and "every entry type" over `main` + `orgwide`.
2. **Decertification batch.** `unassignAll(state, device)` (`core/log/rotation.ts`) gives `device_unassign_org` (when
   org-assigned) followed by `device_unassign` for every explicit assignment; both the pending-rotation job and
   `decertificationBatch` use it. The rotation notice counts a device once for either entry.
3. **Assignment to the whole organization.** Offered to admins holding the current `OK`: in the platform-assignment
   dialog the scope select lists the workgroups and "Whole organization" (value `org`; candidates are those of the
   default workgroup), in the workgroup dialog of an org device, and as "Assign to the whole organization" on each
   listed device that is not decertified (its `platform_seq` from the verified log). `buildBatch` wraps `OK` and every
   current `WK` the admin holds; the admin's `CK` is wrapped afterwards as for a workgroup assignment. The dialog shows
   the text of 06-web.md §7.3 verbatim. The select of the platform dialog carries `devices.assign.scope`, its label
   keeps `devices.assign.wg` (Playwright's `selectOption` follows a label to its control), so older specs keep working.
4. **Device rows.** "Whole organization" and the `data-assigned-org` flag come from the verified log, not from the
   hub's `assigned_org`; explicit assignments are listed next to it with their × (they stay after "Remove from the
   organization"). The "Assign to workgroup" button is hidden for an org-assigned device. Capacity details are shown
   only when reported; memory and disk in GiB (one decimal); `sessions_per_vm = 0` reads "no limit", `vms_max = 0`
   "as many as the memory allows". A VM line without a reported limit reads "3 sessions".
5. **Compatibility refusal.** The web client shows the existing `peer_outdated` text of ADR-060 ("A device needs an
   update").
6. **Composer.** The selects `home.device` (`auto` or a device ID) and `home.vm` (`any` or a VM ID, only with a device)
   are shown when the workgroup has devices; preselected with the workgroup default if its device (and VM) is still
   listed. `POST /v1/tasks` cannot express "automatic" against a default (a request without `placement` takes the
   default), so choosing "Automatic" while a default exists sends the task without `placement` and then
   `PUT /v1/tasks/{task}/placement` with nulls (best effort; a session already placed keeps its device). A retry
   re-uses the prepared request only with the same choice.
7. **Send sequence.** `ensureDevice` no longer refuses: when nothing can be assigned (no admin, no platform access, no
   certified candidate) the task is created and waits. The automatic assignment itself is `assignFirstCandidate`,
   reused by "Assign a device" of a task waiting with `no_device` (which shows `no_device_available` with its catalog
   text when there is no candidate). The `409 no_device` retry and the composer's devices link are gone.
8. **Workgroup default.** Writers of a workgroup get "Default device" in its row (dialog with the same two selects,
   `PUT /v1/workgroups/{wg}/placement`); the row reads the default from `GET /v1/workgroups/{wg}` for every workgroup
   the user can read and shows "Default device: Automatic" also for a default whose device is no longer assigned.
9. **Waiting states.** Texts per reason in the countdown catalog; `placement_*` name the chosen device by its label
   (from `task.placement`); "Choose automatically" only for writers of the workgroup; `no_device` offers "Assign a
   device" to admins, everyone else reads "An admin of the organization can assign one".
10. **Where a session runs.** Task header (`task.placement`, `data-device`, `data-vm`) and the sessions tab show
    "On <device label> · vm-…" from the session's `device_id` and `vm_id`.
11. **Audit filter.** The action list gains the six KOR-41 actions of `contracts/enums/audit-actions.json`.
