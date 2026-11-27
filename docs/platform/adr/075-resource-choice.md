# ADR-075: Resource choice – several VMs and devices per workgroup, own choice, waiting instead of refusing

Status: Accepted (2026-11-24)

## Context
- Concept idea 38 ([KONZEPT](../KONZEPT.md#k-idee-38)): at session start the platform chooses a suitable device and Lima VM by
  rights and free capacity, a workgroup can use several VMs on several devices, authorised users can choose device
  and VM themselves, and the choice is deterministic. Idea 35 ([KONZEPT](../KONZEPT.md#k-idee-35)): own hardware can be given
  exclusively to one workgroup or to the whole organization; the assignment steers computing capacity, not data
  access; workgroups keep separate VMs; resources are managed together and capacity-aware. Concept §3 point 5
  ([KONZEPT](../KONZEPT.md#k-3-b5)): several VMs per workgroup on several devices under high load.
- Rules: [E-02](../KONZEPT.md#e-02) (no numeric target, report reached capacity clearly instead of failing), [E-04](../KONZEPT.md#e-04) (the agent can do
  everything the user can), [E-05](../KONZEPT.md#e-05) (missing preconditions are triggered, not explained), [E-11](../KONZEPT.md#e-11) (no unnecessary
  restrictions), interpretation [A5](../KONZEPT.md#a-5) (fixed limits become settings with a generous default and a clear message) and
  [A7](../KONZEPT.md#a-7) (where a task runs its content is in plaintext).
- Built state before this ADR: the hub scheduler (03-api §4.6.2, ADR-013, ADR-049) chose a device deterministically,
  but the runner kept exactly one VM per workgroup and device (database rule `vm_wg_id_live`), and the scheduler kept a
  workgroup on the device with its running VM however many sessions ran there. Nobody could choose a device or VM. A
  device could only be assigned to single workgroups (`device_assign {device, wg}`). A workgroup without an active
  assigned device refused `POST /v1/tasks` with `409 no_device` and stored nothing.

## Decision

### 1. Limits are settings (A5)
| Setting | Where | Default | Meaning |
|---|---|---|---|
| `AGENT_RUNNER_SESSIONS_PER_VM` | runner | 16 | occupying sessions per VM (0 = no limit) |
| `AGENT_RUNNER_MAX_VMS` | runner | 0 | VMs of the device that occupy a slot (0 = only the memory budget decides) |
| `AGENT_HUB_VMS_PER_WG` | hub | 100 | live VMs of one workgroup over all devices (0 = no limit) |

"Occupying" sessions are those in a state of `requested` … `stopping` except `suspended` (the runner's
`ACTIVE_SESSION_STATES`; a suspended session keeps its sandbox but runs no agent). 16 follows from the VM size of
8 GiB and roughly 0.5 GiB per Claude Code session; operators raise it with larger VMs. The runner reports both of its
settings in every capacity report, so the hub schedules with the device's own values; a runner that reports none
counts as "no limit" (the behaviour before this ADR).

> **Addendum (2026-11-26): no fixed number of sessions per VM.** Decision of the client (the same day as ADR-059
> addendum 4, "kein fester Deckel"; at the time 11 tasks of the DFX workgroup ran in one VM): `SESSIONS_PER_VM` is
> only an optional upper bound for special cases, **empty (0) by default**. A running VM takes one more session while
> its own free resources last, measured on the VM (`agent_runner.vm.room`: the guest's `GET /v1/metrics` - RAM
> available less the mean `memory.current` of its sessions, at least 512 MiB (since addendum 3: the largest measured
> session, no floor), must leave 10 % of the VM's RAM; the load
> per vCPU at most `VM_LOAD_MAX_PERCENT`, default 85). A VM that does not answer is judged by the bound alone (since
> addendum 5: it has no room for a new session). The same
> measurement decides whether a subagent fits on its parent's VM. When no running VM has room, the existing order of
> section 4 and of the scheduler applies unchanged - a stopped VM, then a new VM on a free slot of the device, else
> `capacity_exhausted` and the hub places the next attempt (section 7, ADR-013, ADR-049); no new rule. The device
> reports `sessions_per_vm` 0, which the hub's scheduler already reads as "no limit". Specification: 05-runner.md
> §1.3 and §3.3 step 2.

> **Addendum 2 (2026-11-26): every fixed number of what may run at once is removed.** Precision of the client, it
> replaces the "optional upper bound" of the addendum above: "diese anderen deckel alle restlos entfernen, der platz
> soll für das gebraucht werden was halt gebraucht wird". Only the physically free resources of a device decide -
> RAM above `MEMORY_RESERVE_MIB`, disk and quota, CPU load (`CPU_LOAD_MAX_PERCENT`, `VM_LOAD_MAX_PERCENT`); when they do
> not suffice, the request waits or the hub places it on another device. Between the kinds (sessions, work/browser/test
> VMs, later CI) there is no rank and no share: the waiting request of the kind with the fewest active instances on the
> device goes first, on a tie the older one (final rule of the client, 2026-11-26; 05-runner.md §3.8). Removed,
> setting, code and documentation:
>
> | Former limit | Where |
> |---|---|
> | `AGENT_RUNNER_SESSIONS_PER_VM` | runner config; `vm.lifecycle` choice; `subagents.limits` (sessions on the parent's VM) |
> | `AGENT_RUNNER_MAX_VMS` | runner config; `vm.capacity.compute` (`vm_slots_total`), `vm.pool`, enrollment and capacity report (`vms_max`) |
> | `AGENT_RUNNER_WORKVM_SLOTS` | runner config; `workvm.accounts`, `workvm.manager`/`service`/`cli` (ADR-059 addendum 4) |
> | `AGENT_RUNNER_WORKVM_MAX_PER_TASK`, `_OWNER`, `_ORG` | runner config; `workvm.manager` (work, browser and test VMs, 13-work-vm.md §2) |
> | `AGENT_HUB_VMS_PER_WG` | hub config; scheduler allowance (`wg_vm_limit`), device state `vm_new_allowed` |
> | `AGENT_HUB_SUBAGENT_MAX_RUNNING` | hub config, check H4 (ADR-088 §2); runner pre-check and harness line |
>
> An older `runner.env` or hub environment that still sets one of them starts: the value is ignored and the start
> names it once (`config.ignored name=<NAME> reason=fixed_limit_removed`), never `config_invalid`. For older peers the
> contracts keep their fields: the hub publishes `subagent_max_running` 0 (no limit) and no longer sends
> `vm_new_allowed`; the runner reads both past and no longer reports `sessions_per_vm`/`vms_max`; the hub still bounds
> a VM's room by a `sessions_per_vm` an older runner reports. `wg_vm_limit` stays a known waiting reason of stored
> tasks but is no longer given. Unchanged: the hub's rate limits, credit and budget, and the size and time limits of
> single requests (subagent depth and total per task stay settings, ADR-088). The only fixed number of the device
> admission is Apple's: at most 2 macOS VMs per Mac (16-ci-runners.md §8.4); CI has no share of its own either
> (16-ci-runners.md §6.6: `MIN_SHARE`, `MAX_SHARE`, `HEADROOM_INTERACTIVE`, `CI_ORG_SHARE`, `CI_CLASS_SHARE` dropped).

> **Addendum 3 (2026-11-26): a full workgroup VM gets a further VM, it does not grow.** Measurement in the DFX
> workgroup VM (26.11., 18:34): 4 vCPU, 8 GiB RAM, 20 GiB disk; 7 active Claude sessions of about 350-510 MB each at
> about 3 % of a CPU (they mostly wait for the model); 3.6 GB in use, 4.3 GB available, no swap, no OOM, memory
> pressure 0, load 1.4 on 4 vCPU. At that use the VM's memory runs short at about 13-15 sessions - because of the VM's
> fixed size, not of the device's free resources.
>
> - **What one more session needs is measured, no number:** the largest `memory.current` among the VM's sessions that
>   have not ended (`agent_runner.vm.room`; the former floor of 512 MiB per session and the mean are gone). A session
>   costs little CPU, so in practice the VM's RAM decides; the load per vCPU (`VM_LOAD_MAX_PERCENT`) stays the second
>   check.
> - **Decision: a further VM of the workgroup, no growing.** When no running VM of the workgroup has room, the existing
>   order of section 4 applies: a stopped VM of the workgroup, then a further VM on a free slot of the device, admitted
>   by the device admission (05-runner.md §3.8), else `capacity_exhausted` and the hub places the next attempt on a
>   device where it fits (ADR-013, ADR-049). The VM is not grown: Lima with Apple's Virtualization framework fixes a
>   VM's RAM and CPUs when it starts - `limactl edit --memory/--cpus` works only on a stopped instance, and the
>   framework has no memory hot-plug (its balloon device can only give back memory below the size the VM started
>   with). Growing would mean stopping the VM and with it every session running there, which the rule "running work is
>   never ended" forbids.
> - **Not chosen:** starting every VM larger and shrinking it with the balloon (Lima does not expose the balloon, and
>   the host would count the full size against its budget); restarting a VM with more RAM once it has no session
>   (possible later through `golden_switch`-like moves, but a second mechanism besides the existing order).
> - Specification: 05-runner.md §3.3 step 3.

> **Addendum 4 (2026-11-26): the host reserve is a measured base need plus a share per VM.** `MEMORY_RESERVE_MIB` was
> a fixed 32 GiB on platform devices; a Mac mini of 32 GB would have kept nothing for VMs. A first correction applied
> the own devices' quarter rule (ADR-074) to every device; the closer measurement on the platform device replaced it
> (processes without the VMs about 7.2 GB: `limactl` 1.96 GB for 9 VMs, the store's PostgreSQL 1.73 GB, the runner
> 0.23 GB, macOS services about 1 GB; wired 6.6 GB, growing with RAM and VMs): the reserve is a base need of 4 GiB
> (macOS, runner, store - measured, not estimated; `vm.capacity.HOST_BASE_MIB`) on every device, plus the host share of
> each VM, about 0.2 GiB for its Lima helper (`VM_HOST_MIB` = 208 MiB), which the admission counts when the VM is
> admitted (`vm_footprint_mib`). A set `MEMORY_RESERVE_MIB` replaces the base need; the installer of own devices no
> longer writes one. The store's PostgreSQL runs with its defaults (128 MB shared buffers); its 1.73 GB is resident
> size summed over its processes, the shared buffers counted per process - measured with the same defaults, 8 busy
> connections show 305 MiB resident but hold 50 MiB (proportional) - so a small device needs no smaller buffers
> (05-runner.md §1.3.1). The CPU has no
> fixed reserve either: the admission starts new VMs while about 1-2 cores stay free for macOS, the runner and the
> VMs' input and output (an eighth of the cores, at least 1, at most 2, `vm.capacity.host_cpu_ratio`);
> `CPU_LOAD_MAX_PERCENT`, empty by default, replaces that rule when set. 05-runner.md §1.3, §1.3.1, §3.4, §3.8.

> **Addendum 5 (2026-11-27, HOT-32): look first, then place; small VMs, more of them.** Finding on the platform
> device at about 10:00: every session of a workgroup ran in one VM of 4 vCPU and 8 GiB - the device ran without the
> measured room of the addenda above, and `SESSIONS_PER_VM=0` meant unlimited room, so no second VM ever started -,
> 11 sessions at a load of 4.3 on 4 cores, tool calls timing out, while the device (36 cores, 512 GB) was 70 % free.
> Client: "die verteilung muss dynamisch sein nach bedarf, nicht auf eine spezifische zahl"; "du kanst doch nicht
> einfach laufende sessions beenden"; "du musst schon erst kucken hats noch platz und wenn nicht dann eine neue vm
> nehmen".
>
> - **Room check before every placement of a new task** (05-runner.md §3.3.1): the runner measures a running VM
>   before it places a new task there; the task fits while one more task as large as the largest one on the VM (its
>   main session and its subagents, measured) still fits - RAM left ≥ 10 % of the VM, load per vCPU of the VM's own
>   vCPUs ≤ `VM_LOAD_MAX_PERCENT` (85). A VM whose guest does not answer in 2 s is **not** a VM with room any more (it
>   was in addendum 1): that is the overloaded VM of the finding. No room: another VM with room, a stopped VM, else a
>   new VM from the pool or a clone through the device admission; else `capacity_exhausted`.
> - **Nothing running is refused**: subagents run in the VM of their task and are no longer refused when it is full
>   (`subagents.limits.vm_room_refusal` is gone, also for resuming a suspended child; ADR-088 §2 "Room of the VM"
>   replaced). Their reserve is the "one more task as large as the largest" kept at the placement of new tasks.
>   Preferred VMs (the user's choice, the parent's VM, the VM of `resume_from`) are taken without refusal even when
>   full - the task's work lives there - and measured for the log. Not chosen: a fixed number of subagents reserved per
>   task, a second lower threshold for new tasks, refusing subagents above a hard bound.
> - **One small size, more VMs** (05-runner.md §3.3.2): client, 2026-11-27: "die grösse der vm sollten wir zum start
>   immer gleich halten für claude sachen und eher klein, lieber mehrere vm". Every session VM keeps `VM_CPUS` /
>   `VM_MEMORY_MIB` at 4 vCPUs and 8 GiB; more need means more VMs through the room check. Not chosen: larger VMs
>   (16 vCPUs / 32 GiB) or a size per device. A size per workspace learned from experience is a separate concept.
> - **Never end a running session**: a size change applies to new VMs only. A VM of an older size keeps its sessions,
>   takes no new one and stops when its last session ended; a stopped one is replaced when its sessions may move
>   (ADR-093 rule), pool VMs of an older size are replaced as soon as the refill runs. A restart of the runner ends no
>   VM and no session (05-runner.md §1.1). Not chosen: growing a VM (addendum 3) and moving running sessions.
> - **Visible**: every placement is logged with the chosen VM, the measured room and the reason
>   (`runner.vm.placement`) and counted (`agent_runner_vm_placements_total{reason}`,
>   `agent_runner_vm_room_checks_total{result}`, 08-monitoring.md).

### 2. Capacity report (runner → hub, additive)
The `capacity` object of the heartbeat gains optional counters: `cpus`, `memory_total_mib`, `disk_free_mib`,
`vms_running`, `sessions_active`, `work_vms_running` (the work VMs of the runner's other areas, reported through the
runner extension point `<area>/capacity.py:USAGE`, which also lets them claim slots and memory), `sessions_per_vm`,
`vms_max`. `vm_slots_total` already honours `vms_max` (`min(budget / VM_MEMORY_MIB, vms_max)`). The enrollment body
keeps the five original counters (it is signed and copied into `device_certify`). The capabilities go with the
mechanism of ADR-060: the runner announces `log.org_assign` and `vm.choice` in the `Agent-Features` header of its live
channel (`device.features`).

### 3. Device choice (hub, deterministic)
Candidates are the active devices assigned to the workgroup (directly or through the organization, §5) that are
active members of the org. For each candidate the hub knows from `hub.vm` and `hub.session` the workgroup's live VMs
on that device with their occupying sessions, and the sessions of the workgroup placed there that have no VM yet
(*pending*). The *room* of a VM is `sessions_per_vm − occupying sessions` (unbounded without a limit); pending sessions
use the room of the running VMs first.

- Level 1: a running VM of the workgroup with room left after the pending sessions.
- Level 2: a stopped VM of the workgroup with room (a free slot is needed only after a refusal, ADR-049).
- A VM of the workgroup in another state (being assigned, stopping) counts at level 3 with its room and needs no slot
  (the same refusal rule as a stopped VM); pending sessions use the room of running VMs first, then of VMs in another
  state, then of stopped ones.
- Level 3: a new VM: a ready pool VM (`pool_ready > 0`, it holds its slot already) or `vm_slots_free > 0`, the workgroup is below `AGENT_HUB_VMS_PER_WG` and, when the device reports
  `disk_free_mib`, at least `VM_DISK_GIB` of 20 GiB (20480 MiB) are free.
- A device assigned only through the organization (§5) also needs the wrap of the workgroup's current key for itself
  (`hub.wrap`); until a key holder has added it the device is skipped with the reason `keys_pending`. Explicit
  assignments carry that wrap in their own batch.
- Otherwise the device is skipped. Order: (level, most free slots, most free VM memory, smallest device ID), as
  before. Every placement within one re-scheduling round is booked on the chosen device (a pending session; at
  level 3 also a slot and a VM of the workgroup), so equal inputs give equal choices and one free slot does not attract
  every waiting session.
- With `sessions_per_vm = 0` every running VM always has room: the order is exactly that of 03-api §4.6.2 before.
- The hub tells the runner per session whether it may create a VM for it (`vm_new_allowed`, false while the workgroup
  is at `AGENT_HUB_VMS_PER_WG`), so the runner's step 5 (§4) keeps the hub's limit.
- The projection `wg_device` holds the effective assignments (explicit and through the organization; a `wg_create`
  adds rows for the organization's devices), so presence and capacity signals of such a device and the log signal of
  every entry re-schedule the waiting sessions as before.

### 4. VM choice (runner, deterministic)
`ensure_vm` takes the session and an ordered preference and holds the workgroup lock until the session row names
its VM (so two concurrent starts never both take the last place of a VM):
1. the VM the hub names for the session (`vm_id_pref`, a user's choice, §6), if it is a live VM of **the session's
   workgroup** (the runner checks the workgroup of every preferred VM; the guest health check confirms it again) –
   without regard to the limit (the hub placed it only when the VM had room);
2. for a subagent the VM of its parent session, for a resumed main session (`resume_from`) the VM of that session –
   the same workgroup check, without regard to the limit (the work lives there);
3. a running VM of the workgroup with room: most room first, then the smallest VM ID;
4. a stopped VM of the workgroup with room (restart; the golden replacement rule of ADR-009 stays), smallest ID;
5. a new VM (pool, then clone) when the hub allows it (`vm_new_allowed`) and a slot is free (`AGENT_RUNNER_MAX_VMS`,
   memory budget, LRU stop of another workgroup's idle VM as before);
6. otherwise the session fails `capacity_exhausted` and the hub places the next attempt (ADR-049).

The database rule "one live VM per workgroup" (`vm_wg_id_live`) is dropped; isolation between workgroups stays (a VM
belongs to exactly one workgroup for its life). Unassigning or archiving a workgroup deletes all its VMs.

### 5. A device for the whole organization
- New membership-log entries `device_assign_org {device, platform_seq?}` and `device_unassign_org {device}` (admin;
  with `platform_seq` only if the org has platform access, as for `device_assign`). While a device is assigned to the
  organization it is assigned to **every** workgroup of the org, present and future (archived ones stay readable).
  `device_assign_org` is allowed while the device has explicit per-workgroup assignments; the state is their union,
  and the explicit assignments remain after `device_unassign_org`. A per-workgroup `device_assign` of an organization
  device is refused (it is assigned already), a `device_unassign` needs an explicit assignment. `remove` ends both.
  After a decertification the admin batch owed by the org (04-crypto §4.7) is `device_unassign_org` (when assigned to
  the organization) plus `device_unassign` for every explicit assignment, plus R. "Exclusive for one workgroup" stays
  the per-workgroup `device_assign`. Clients decide which devices may receive keys only from signed entries of the
  verified log, never from the hub's lists (`assigned_via`, `assigned_org` are display data).
- Rights and keys follow from "assigned": the device reads the org scope and every workgroup scope and is eligible for
  the credential key of every member (04-crypto §2, §6.3 W2). The rotation obligation R needs no new rule: after
  `device_unassign_org` it is every workgroup the device can no longer read and the org scope when nothing is left.
- What is distributed, and when:
  - **at the assignment** – the admin's batch carries the entry and wraps of the current `OK`, of every `WK` the admin
    holds (since ADR-058 an admin reads every workgroup, so normally all of them) and of the admin's own `CK`;
  - **right after** – every other key holder adds what it may (catch-up, 04-crypto §5.11): members' clients wrap the
    `WK` of their workgroups and their own `CK`, devices already assigned wrap the workgroup keys they hold. A
    workgroup whose keys reach the device only later waits for them like any device that lacks a wrap;
  - **for a new workgroup** – the `wg_create` batch wraps `WK_1` for the organization's devices (they are recipients);
  - **at every rotation** – new epochs are wrapped for them like for every other reader.
- Honest statement (A7, concept §5 point 4 [KONZEPT](../KONZEPT.md#k-5-b4)): "The assignment steers computing capacity, not
  data access" holds for people – nobody gains access to a workgroup through it. The device itself must decrypt
  what it runs: a device given to the whole organization holds the keys of every workgroup and can read their tasks,
  chats and whiteboards and the credentials of their members, not only while a task runs there. Specification and
  the assignment dialog say so; whoever wants to keep a workgroup off a device assigns the device per workgroup.
- Rotation: `device_unassign_org` makes R every workgroup of the org (archived ones included) and the org scope,
  unless explicit assignments remain; like every rotation today it is one atomic batch built by an admin, who reads
  every workgroup anyway (ADR-058) and wraps the new epochs for every remaining reader.
- Compatibility: older runners and agent tools do not know the new entries and would refuse the org log. The hub
  therefore refuses a batch (`409 peer_outdated` with `detail.devices`, as for `admit` in ADR-060) that adds an
  assignment to the organization, or that adds a device (other than by its own `accept`: an enrolling runner verifies
  the served log before it accepts) to an org that uses one, unless every active device member of the org after the
  batch has announced `log.org_assign`. Removals and owed rotations are never blocked. The agent tool
  of the same release knows the entries; installing it is a rollout step (users of an older tool get `log_type` on
  that org until they update).

### 6. Choosing device and VM
- `POST /v1/tasks` takes an optional `placement {device_id, vm_id?}`; without one the workgroup's default applies
  (`GET`/`PUT /v1/workgroups/{wg}/placement`, `wg:write`, audit `wg.placement`), otherwise the choice is automatic.
  Who may write the workgroup may choose (E-11): the user, and therefore the user's agent
  (`agent-platform task create … --device … [--vm …]`).
- The device must be a candidate of the workgroup (assigned directly or through the organization, active, member):
  otherwise `403 placement_forbidden` – nobody chooses a device not assigned to the workgroup. A VM must be a live VM
  of that workgroup on that device (`409 placement_invalid`). A default whose device is no longer a candidate is
  ignored (automatic), never an error.
- The choice is stored with the task (`hub.task.placement_device_id`, `placement_vm_id`) and holds for all of its main
  sessions: the scheduler only considers that device (and, with a VM, only that VM's room); a chosen device that is
  offline or full means waiting for it, with the reasons `placement_offline` / `placement_full` and the action
  "Choose automatically" (`PUT /v1/tasks/{task}/placement`, `task:write`, which also changes the choice of a waiting
  or later session). When the device stops being a candidate the task falls back to the automatic choice; when the
  chosen VM is no longer a live VM (deleted, replaced by a newer golden) only the device counts – for the task and for
  a workgroup default alike. The hub hands the chosen VM to the runner as `vm_id_pref` in `GET /v1/device/state`.
- Where a session runs is visible everywhere: task and session views show device and VM (existing `device_id`,
  `vm_id`), device lists show every VM with its workgroup and sessions, the agent tool prints them.

### 7. "Full" and "no device" are states, not errors
`POST /v1/tasks` always creates the task. Without a placement the task stays `scheduling` with one of these reasons
and the hub places it by itself as soon as possible (signals of presence, capacity, log and placement; 15 s sweep):

| Reason | Category (situation view) | Meaning and follow-up |
|---|---|---|
| `capacity_exhausted` | `waiting_for_capacity` | every candidate is full; starts by itself |
| `wg_vm_limit` | `waiting_for_capacity` | the workgroup reached `AGENT_HUB_VMS_PER_WG`; starts when one of its VMs has room or the operator raises the setting |
| `device_offline` | `waiting_for_device` | assigned devices are offline; starts when one connects |
| `placement_full` | `waiting_for_capacity` | the chosen device (or VM) is full; action "Choose automatically" |
| `no_device` | `waiting_for_device` | no device is assigned; the task view offers "Assign a device" (admins) with the candidates, everyone else sees who can; the org's admins see it in their lists of waiting tasks |
| `placement_offline` | `waiting_for_device` | the chosen device is offline; action "Choose automatically" |
| `keys_pending` | `waiting_for_device` | the only devices with room are assigned through the organization and lack the workgroup key; a key holder's client adds it (catch-up) and the task starts |
| `credit_exhausted` | `waiting_for_credit` | the organization's credit is used up (KOR-31); platform devices are skipped, own devices still run; starts once credit is added |
| `platform_access_required` | `waiting_for_device` | no own device and the organization has no access to the operator's devices; the operator sees it (`agent-hub platform waiting`) |

Tasks waiting with `no_device` and no candidate, or with `platform_access_required`, are listed for the operator
(`GET /v1/platform/waiting`, root-signed; `agent-hub platform waiting`). The code `no_device` stays in the error
enumeration for older clients but is no longer answered.

Refusals (ADR-049, amended): the number of refusals in a row is a setting (`AGENT_HUB_CAPACITY_REFUSALS`, default 3).
Reaching it no longer fails the task: the next attempt is created as before but waits (`capacity_exhausted`) and is
not placed before `AGENT_HUB_REFUSAL_HOLD_SECONDS` (default 60) have passed (`hub.session.not_before`), so a device
whose reports lag cannot make the hub loop faster than once a minute; the task starts by itself once there is room.

## Consequences
- With one execution device in operation the change is: a workgroup with more than `AGENT_RUNNER_SESSIONS_PER_VM`
  concurrent sessions gets a second VM on the same device instead of piling into one; tasks are never refused for
  want of a device; device and VM can be chosen; the device can be given to the whole organization. Spreading over
  devices starts with a second device.
- Limits can be exceeded by a race between the hub's view and the runner (a session placed on the last place of a
  VM while another starts there); the runner then takes the next VM, which is bounded by its slots.
- The org log of an organization that uses `device_assign_org` cannot be read by runners and agent tools of an older
  release (guarded by the feature check for devices, §5; agent tools by the rollout).
- Interpretation for the client (A7): idea 35 says the assignment steers capacity, "not data access". This ADR reads
  it as "no person gains access"; the device itself necessarily holds the keys of every workgroup it may run. The
  report of the package puts this reading before the client.

## Alternatives considered
- The hub chooses the VM too: it lacks the runner's live view (pool, golden versions, in-flight clones); the runner
  stays the authority for VMs and the hub for devices, each with a documented order.
- Organization assignment as a hub-side flag that clients expand into per-workgroup entries: the flag would not be
  signed, so a hub could make an admin's client assign a device to a new workgroup.
- Lazy key distribution (a device gets a workgroup key only when a task of that workgroup is placed there): needs a key
  holder online at placement time and still leaves the keys on the device afterwards; recorded as a possible
  hardening.

## Deviation from DESIGN.md
None beyond ADR-001.
