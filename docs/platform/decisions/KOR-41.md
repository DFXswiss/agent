# KOR-41 – decisions (hub, contracts, specification, web)

Design: [ADR-060](../adr/060-resource-choice.md). Runner and agent tool: the decisions file of the agent repository.

## Hub, contracts, specification

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
7. **Compatibility gate** `device_outdated`: checked for batches that add an organization assignment or a device to an
   org that uses one; the feature comes from the runner's capacity report (`features`).
8. **Who may choose** device and VM: everyone with `wg:write` (E-11), the user's agent included (E-04); the device must
   be a candidate of the workgroup (403 `placement_forbidden`).
9. **E2E number 47**: E2E-41 is taken by KOR-37 and E2E-45/46 by other packages; 47 avoids a collision.
10. **Defaults**: `AGENT_RUNNER_SESSIONS_PER_VM` 16 (8 GiB VM, about 0.5 GiB per Claude Code session),
    `AGENT_RUNNER_MAX_VMS` 0 (memory decides), `AGENT_HUB_VMS_PER_WG` 100.
11. **Numbers after the rebase**: ADR-060 (058 and 059 went to KOR-34 and KOR-29 meanwhile), hub migration 0013.
12. **Independent review, handled:** H1 (a device joining by its own `accept` is exempt from the `org_assign`
    check), M1 (removals and owed rotations are never blocked: the check runs only when an organization assignment or a
    device other than by `accept` is added), M2 (a device assigned only through the organization also needs the wraps
    of the current `OK` and of the task owner's current `CK`), L3 (`keys_pending` for a chosen device without keys),
    L5 (a stored wrap for a device emits `wraps.stored`; the scheduler re-runs), L6 (other VM states documented in
    ADR-060 §3), L7 (`capacity.py:USAGE` added to ADR-027's table), M3 (E2E-47 asserts the deterministic candidate).
    Kept as they are, with the reason: L1/L2 (the golden replacement and extra stopped VMs of a workgroup follow the
    existing rules of ADR-009 per VM; a stopped VM gives its RAM back and its disk is the existing VM disk budget –
    reclaiming idle extra VMs is a later tuning), L4 (an unknown `vm_id` names a VM the hub has not seen yet; the
    following `vm_state` report creates the row and the scheduler counts the session by `vm_id` from then on), L8 (the
    runner copy is regenerated from the final agent-core commit at merge time).
13. **The work VMs of KOR-29 report through the discovered point** `workvm/capacity.py:USAGE` (a new file in that
    area, adding nothing to its code): the package text requires that work VMs count against the capacity.

## Web

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
5. **`device_outdated`.** The catalog title is the message itself ("This device's runner must be updated first"),
   because the action dialogs show only the title.
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
