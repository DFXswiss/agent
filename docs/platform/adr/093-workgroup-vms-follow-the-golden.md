# ADR-093: Workgroup VMs move to the current golden by themselves, paused sessions move with them

Status: Accepted (2026-11-25)

Implements [Idee 40](../KONZEPT.md#k-idee-40) (the original session with its full context continues, also on another
VM), [Idee 39](../KONZEPT.md#k-idee-39) (a VM is kept while sessions use it), [Idee 38](../KONZEPT.md#k-idee-38)
(deterministic choice), [Idee 32](../KONZEPT.md#k-idee-32) (autonomous first: nobody has to act),
[E-02](../KONZEPT.md#e-02) (say clearly what waits instead of failing), [E-04](../KONZEPT.md#e-04) and
[E-26](../KONZEPT.md#e-26) (the switch by hand is a command as well as a button), [E-19](../KONZEPT.md#e-19) (exclude
the class: no workgroup stays on an old golden because it is used) and [E-25](../KONZEPT.md#e-25) (no new protective
checks). Builds on ADR-072 (context checkpoints and restore on another VM), ADR-075 (resource choice) and KOR-67 (a
device builds its golden).

## Context
- A workgroup VM was replaced by a clone of the newest ready golden only at its next start, only while it was stopped
  and only when no session on it was `suspended` (05-runner.md section 3.3 level 3). A used workgroup always has
  suspended sessions, so its VM never moved: in operation (2026-11-25) the VM of the DFX workgroup ran on the golden
  of 2026-11-24 14:16 although eight newer ones had been built, and the first real task there used old rules and
  tools (test runs, browser tools, speaker line, `merge_base`).
- The rule dated from before ADR-072: the files of a paused session lived only on its VM. Since KOR-33 the runner
  checkpoints every main session (transcript and workspace) when it becomes `idle` or `suspended`, every 10 min while
  it works and before its VM is stopped or deleted, and a session resumed on another VM is restored from that
  checkpoint (`prompts.resume`, `contexts.restore`).

### What a checkpoint carries (checked in the code, KOR-70)
| Part of a paused session | In the checkpoint? | Evidence |
|---|---|---|
| Conversation (transcript, todos, file history, companion directory) | yes, complete lines | `agent_guest.contexts._transcript`, `complete_length` |
| Workspace files, also never committed ones | yes: a walk of the file system, not of git | `agent_guest.contexts._workspace` (`work/<path>`), regular files of the session user, directories, links inside |
| Repository state | yes as files: a checkout is a plain tree without `.git` (`github.checkout`, `files/import`); `.git` can never exist in a workspace (`policy.PROTECTED_DIRS`), commits and pushes are platform operations | `agent_runner.github.checkout`, `agent_guest.policy` |
| Large workspaces | only up to `AGENT_RUNNER_CONTEXT_WORKSPACE_MIB` (4096) and `files.MAX_ENTRIES` (100 000 entries - as likely to be reached by one large checkout): above that the workspace is left out (`workspace: omitted_too_large`) | `contexts._workspace`, `context.json` |
| Links leaving the workspace, devices, FIFOs, files of another owner, unreadable entries, protected names | left out and counted (`workspace_omitted` > 0, restore state `partial`) | `_ContextExport.skip`, `archive.Context.workspace_state` |
| Background processes of the session | no - but they do not survive a pause either: the guest stops the session's unit when it suspends the session | `agent_guest.sessions` (suspend) |
| The session's home outside the conversation's own files (caches, tools installed there, Claude Code's project memory or plans below `.claude`) | no - tool state; the new golden brings the tools. The same holds for every restore on another VM since KOR-33 | `agent_guest.contexts` (`TRANSCRIPT` members) |
| Work VMs (test, browser) of the session | not needed: separate Lima instances, not tied to the workgroup VM; the mirror is synchronised again by its manifest | `workvm.manager`, 13-work-vm.md |
| Child sessions (subagents) | no checkpoint; they live in their parent's VM and are never moved | `prompts.resume` (child sessions), ADR-088 |
| Whether a checkpoint was complete | the log line `checkpoint_taken … workspace=… omitted=…`, not stored; a request that ended without a checkpoint (`dropped`: transcript too large, export refused, eight failed attempts) left the older checkpoint in place as if it were current | `checkpoint.Checkpointer._checkpoint`, `store.drop_due` |
| The main orchestrator session (ADR-082) | never checkpointed and never suspended while it is the orchestrator | `0031_orchestrator.sql`, `store._NOT_ORCHESTRATOR` |

The reason "unsaved files of paused sessions would be lost" no longer holds for main sessions whose newest checkpoint
is complete; it still holds for an incomplete workspace (too large, partly left out), a checkpoint that is due,
missing or dropped, for child sessions (they have their own workspace and no checkpoint) and for the orchestrator
session (it never pauses). That is the rule below.

## Decision

### 1. When a VM is ready to move
A live workgroup VM whose `golden_version` is not the newest `ready` golden of the device is **outdated**. It is
**ready** when every session on it that is not `stopped` or `failed` is a main session in state `suspended` whose
checkpoint is complete:

- no checkpoint of it is due (`runner.context_session.due_at` NULL),
- its newest checkpoint request ended with a checkpoint (`archive_sha256` set and not `dropped`) whose newest object is
  in the local cache (`runner.context_object` of kind `context`, any state: the local copy is complete; the newest
  object of every task is never evicted) and no restore from that object failed on this device before
  (`restore_failed`),
- the workspace of that checkpoint was `full` or `empty` (new column `runner.context_session.checkpoint_state`,
  written with every taken or identical checkpoint, `dropped` when a request ended without one, migration
  `0034_golden_switch`).

Otherwise it is **held**, with one entry per cause (`holds`, at most 16, sessions in ID order):

| `reason` | Who holds | Ends when |
|---|---|---|
| `session_active` | a session that is not paused (`session_id`), also the orchestrator session | it is paused, stopped or failed |
| `subagent` | a child session that is not stopped or failed (`session_id`): it has its own workspace and no checkpoint, and it never moves (ADR-088) | its parent stops it or ends |
| `checkpoint_due` | a paused session whose checkpoint is still due (`session_id`) | it is taken |
| `checkpoint_missing` | a paused session without checkpoint or without its local object (`session_id`) | a checkpoint is taken |
| `workspace_incomplete` | a paused session whose newest checkpoint left (part of) the workspace out (`session_id`) | a later complete checkpoint |
| `capacity` | the device has no free slot for the new VM without stopping another workgroup's VM | a slot is free |
| `switch_failed` | the last attempt could not make the new VM ready | the next attempt (5 min, doubling, at most 1 h) |

`since` of an entry is the time the runner first saw the cause (`runner.golden_switch_hold`, kept across restarts).
A paused session whose checkpoint is due or missing - also one checkpointed before this migration (`checkpoint_state`
NULL) or whose final checkpoint the stop did not finish within its 120 s - gets a fresh checkpoint: a running VM is
stopped with its final checkpoints (§2), a stopped VM is started for it when a slot is free without stopping another
workgroup's VM (attempts 5 min apart, doubling, at most 1 h; the export is fast when nothing changed: `identical`).

### 2. The switch (runner, `vm.golden_switch`)
Under the workgroup's lock, so no session start chooses the VM meanwhile:

1. judge again (section 1); a running VM is stopped first (`_stop`: the retire hooks take the final checkpoints), then
   judged again - a hold now leaves the VM stopped and as it is;
2. the new VM: a pool VM of the newest golden, else a clone, assigned to the workgroup, its guest healthy
   (`running`). Another workgroup's VM is never stopped for it (`capacity`);
3. only then the old VM is deleted (`limactl delete`, row `deleted`, `vm_state` with `replaced_by`), and
   `runner.golden_switch {old_vm_id, new_vm_id, golden_version, at}` is recorded. A failure in step 2 deletes nothing
   (`switch_failed`).

The sessions keep their `vm_id`. Their next prompt resumes them (05-runner.md section 6.3): the old VM is gone, so the
resume chooses a VM of the workgroup - normally the new one; in a workgroup with several VMs the running one with the
most room, which may be another outdated VM that then moves in its turn - and restores the session there from its
checkpoint (ADR-072 section 7). The restore note and the audit say so (section 4). A session that has no checkpoint
never gets here: its VM stays as it is.

A prompt that arrives during a switch: the resume sets `resuming` without the workgroup lock, but the switch judges
again under the lock from the session rows; a session that resumes before that judgement holds the VM, one that
resumes after it waits for the lock and then restores on the new VM from the final checkpoint the switch took. The
switch holds the workgroup's lock for the final checkpoints (at most 120 s) and the new VM (a pool VM in 0.1 s, a
clone in about 6 s); a start of that workgroup waits meanwhile - accepted, it happens once per golden.

The pass runs in task `vm-pool` after every refill (every 60 s and after every assignment) and after a desired state
that asks for a switch (section 5; reconcile step `vm_switch`, order 85). The existing replacement at a start (05-runner.md section 3.3 level 3: a stopped
outdated VM with room) follows the same rule: it replaces a VM whose suspended sessions are all ready, and creates
the new VM before it deletes the old one.

### 3. Visibility
`vm_state` (outbox) gains the optional object `upgrade` and the optional `replaced_by` (contracts, additive):

```
upgrade: {golden_version, golden_built_at, state: "waiting" | "switching", holds: [{reason, session_id?, since}],
          sessions: [session_id]}   // sessions: the paused main sessions that move with the VM
replaced_by: vm_id                  // on the `deleted` report of a switched VM
```

The runner reports an outdated VM's status whenever it changes (`upgrade: null` once current) - only to a hub that
knows the fields: the hub's desired state always names `vm_switch` (section 5), an older hub refuses unknown fields
(`additionalProperties: false`), so a runner that has not seen `vm_switch` sends neither field (nor `moved` of
section 4) and the switch itself still happens. The hub stores it
(`hub.vm.upgrade`, `hub.vm.replaced_by`, migration `0035_golden_switch`) and shows it to `wg:read` in
`GET /v1/workgroups/{wg}` (`devices[].vms[]` gain `golden_version` and `upgrade`). When a switched VM is reported
deleted, a workgroup default (`wg_placement.vm_id`) or task choice (`task.placement_vm_id`) naming it names the new VM
- not needed for correctness (a deleted VM is ignored as a choice), but a member's choice stays a choice.
The web shows on the workgroup screen per outdated VM "New version waits" with the holds ("Session … is working since
…") or "moves when the sessions are paused", and the CLI `agent-platform wg vm-switch --wg W --vm V --preview`
prints the same.

### 4. What the user notices
The restore note of the moved session (`restored_line`, place `golden`): "This is your own earlier conversation of
this task, continued from its checkpoint of … on the new version of the workgroup VM (it was updated while the session
was paused); your files were carried over: your workspace was restored as it was." Without the work-VM sentence of
the other places: work VMs are separate and still there.
The audit `session.context_restored` gains the optional `moved: "golden"`; the hub stores it on the session as
`context_reason = golden_switch` and the task shows one line: "Continued on the new version of the workgroup VM -
files carried over."

### 5. By hand
`POST /v1/workgroups/{wg}/vms/{vm}/switch` (body `{}`, browser or agent key; `wg:read` and `org:admin` of the
workgroup's organization - the member with administration rights; `403 forbidden` without `org:admin`, `404
not_found` when the VM is no live VM of the workgroup) asks the device to switch now. While the device reports the VM
outdated (`upgrade` set) it stores `switch_requested_at` (kept when already set) and `switch_requested_by` and sends
the device the hint `reconcile`; for a current VM it stores nothing (no new error code: `requested: false`, E-26).
Either way the write is audited (`vm.switch_requested {vm_id, device_id, requested}`). The answer is the preview `{vm_id, device_id, golden_version,
requested, requested_at, upgrade}`: holds and the paused sessions that move along. The desired state names the
requested VMs of the device (`vm_switch: [vm_id]`, new section, until the VM is current or deleted); the runner's
reconcile step `vm_switch` wakes the pass, which takes requested VMs first and skips the backoff of an earlier failed
attempt. The same rules apply - a request never moves a VM with a running session or without complete checkpoints
(Abgrenzung of KOR-70); it only saves the wait. Command: `agent-platform wg vm-switch --wg W --vm V` (with
`--preview`: only the status); button "Switch now" on the workgroup screen with the preview as confirmation.

### 6. What does not change
No VM with a running session is ever switched; nothing is deleted before the new VM runs; no new protective check
(E-25). Child sessions keep their VM. The hub learns no content (only IDs, reasons and times).

## Remaining risks
- A restore can still fail after the switch (e.g. a Claude Code of the new golden older than the transcript's, or a
  damaged cache that the hub does not hold): the reported emergency path of ADR-072 section 7 applies. The rule above
  excludes what is known beforehand (a failed restore of the same object, a dropped or incomplete checkpoint).
- Child sessions hold their VM until their parent stops them or ends (ADR-088): a workgroup that keeps paused
  subagents stays on its golden, visibly (`subagent`). Moving them needs checkpoints of child sessions with their own
  cache rule (the cache keeps the newest object per task, a child's object would push out its parent's); left to a
  later package.
- The orchestrator session never pauses: its VM stays while it is the orchestrator (`session_active`).

## Consequences
- A used workgroup reaches the newest golden at the latest one pass after all its sessions are paused with complete
  checkpoints; one that keeps a session working stays and says why.
- A session paused with a workspace above 4 GiB keeps its VM on the old golden until its workspace is checkpointed
  completely or the session ends (visible as `workspace_incomplete`).
- E2E-69 (T1): a VM on an old golden with two paused sessions whose files changed is switched; both continue on the
  new VM with their files.

## Addendum HOT-24 (2026-11-26): nothing to save, never savable, no loop

### Finding
After release 34 three workgroup VMs had stood still on goldens of 2026-11-23 and 2026-11-24 since KOR-70, each held
by `checkpoint_missing`. Every export for their sessions ended `checkpoint_dropped … export_refused error=not_found`.
Checked in the code:

- The route `POST /v1/sessions/{id}/context/export` came with KOR-33 (`69173353b`). The guest version 0.2.0 came with
  `db40d8bc8`, which does not contain that commit: 0.2.0 exists with and without the route. A guest without the
  route answers like for an unknown session: `http.server` turns an unknown path into `404` and the guest's
  `send_error` names it `not_found` (`agent_guest.routing`); the export route itself answers `not_found` for an
  unknown session.
- `Checkpointer._drop` wrote only `checkpoint_state = 'dropped'` and a log line; `store.mark_vm_due` made the session
  due again at every `flush_vm`; `checkpoint_holds` judged `dropped` as `checkpoint_missing`, which a fresh checkpoint
  is supposed to end (`REFRESHABLE`).
- `GoldenSwitch._refreshes` counted the starts for fresh checkpoints in memory only: after every runner restart (every
  release) three more starts and stops of each such VM, each ending `checkpoint_dropped`, `vm_flushed`.
- `VmManager._from_stopped` restarts a held stopped VM for a new session: a new task of such a workgroup ran on the
  old guest.
- A session that never completed a turn has nothing to save (the guest refuses its export `state_conflict`, the
  runner refuses its archive `context_incomplete`), but it held its VM like any other.
- A moved session without any checkpoint (`prompts.resume` on another VM, `contexts.restore.amend`) takes the
  existing path of a session without checkpoint: `context_missing`, a fresh conversation told to read the task's
  messages, audited `session.context_fallback`. That path works for these sessions unchanged.

### Decision
1. **Nothing to save holds nothing.** A paused main session holds its VM only when it has *worked*: some prompt was
   accepted for it on this device (`runner.prompt_delivery` in state `accepted`; `prompt_accepted` is what makes a
   session `working`, the guest accepts only prompts the runner registered, initial prompts included, and the rows are
   never deleted - after a store wipe `adopt_delivered` restores them -, so this holds for sessions from before this
   addendum too). A request that ends because the guest refuses `state_conflict` or because the archive is
   `context_incomplete`, for a session that never had a checkpoint here (no archive hash and no cached object of it),
   is stored as `checkpoint_state = 'nothing'` and holds nothing either while no new request is due. Such sessions
   move with the VM (`sessions`); their next prompt starts them on the new VM by the existing rule for a session
   without checkpoint (`contexts.restore`: `context_missing`, a fresh conversation that reads the task's messages,
   audited). A due checkpoint of a session that never worked keeps no stopped VM from being replaced (`vm_pending`).
2. **Never savable is remembered.** An export refused `not_found` or `size_exceeded` ends the request
   `checkpoint_state = 'impossible'` with the reason (`impossible_reason` `export_refused:<error>`), the number of
   accepted prompts at that moment (`impossible_turns`) and the VM (`impossible_vm_id`; migration
   `0037_checkpoint_impossible`); the judgement is `checkpoint_impossible` while the session is on that VM - after a
   move its new guest may well export it, so there the mark no longer counts (independent review). Both refusals stay as long as the guest stays: `not_found` from the running guest of the
   session's own VM means the guest does not know the session or has no export route (it keeps its sessions in its
   state database), and a transcript over its limit only grows. *Why not the guest version:* 0.2.0 exists with and
   without the route; a version rule would be wrong in one direction or the other. The guest's own answer for this
   session is the evidence. *Not* impossible (design review): the guest's `unavailable` (a passing fault, now
   postponed like an unreachable guest), attempts that ran out (unreachable guest, missing epoch key: passing causes),
   other refusals and refused archives - they stay `dropped` (`checkpoint_missing`); the bounded, persisted refreshes
   of point 3 keep them from looping. This narrows the package's example "aufgebrauchte Versuche" on purpose: a
   passing fault must not freeze the last turn of a session that worked.
3. **No loop.**
   - `mark_due`, `mark_periodic`, `mark_vm_due` (so `flush_vm`) and therefore `due_on_vm` leave out a session whose
     state is `impossible` while it has no more accepted prompts than `impossible_turns` and is still on that VM: once
     it works again or moved, its next pause makes it due again, and a successful checkpoint replaces `impossible`.
   - A VM with a `checkpoint_impossible` hold is neither started for fresh checkpoints nor stopped for a switch: a
     fresh checkpoint cannot end that hold. Only a stopped VM whose holds are all `checkpoint_due`/`checkpoint_missing`
     is started for them.
   - The counters of the switch survive restarts: `runner.golden_switch_attempt (vm_id, kind refresh|failure, count,
     reason, next_at)` and the requests by hand taken over, `runner.golden_switch_request (vm_id, requested_at)`, so
     a desired state after a restart is no new request; a request without a time (a hub before this addendum) is
     stored once and never new again while it is listed. A start without a free slot counts no attempt (it was not
     tried); the refresh attempts start again when a request is new or newer, or when the VM ran for its sessions
     (new work may have changed what is due).
4. **The VM stays unless a member asks, and a request covers only what the member saw.** Without a request a VM held
   by `checkpoint_impossible` stays as it is (KOR-70 goal 2), and `_from_stopped` restarts it for a new session as
   before (the package excludes changes to the choice of a VM). A request by hand (`vm_switch`, command and button)
   covers the `checkpoint_impossible` holds the device had *reported* to a hub of `vm_upgrade_version` 2 before the
   time of the request: the hub sends that time (`vm_switch_at`, the newest `POST .../switch`, which now sets
   `switch_requested_at` to its own time instead of keeping the first), the runner compares it with the time it
   first reported the hold to such a hub (`runner.golden_switch_hold.reported_at`, set only for the holds the status
   listed). Such a hub gets up to 64 holds (`vm_upgrade.holds`, before 16 - the incident's VM had 30 such sessions),
   those a request does not cover yet first, so a VM with even more converges with further requests. A session that
   becomes impossible after the request (e.g. a new task that ran on the old guest meanwhile), or one never listed,
   holds the VM until a member asks again - the confirmation always named the sessions it moves (design and
   independent review). The hub's and the device's clocks differ by the usual skew; a hold reported in the seconds
   around a click may need one more request. All other
   holds still apply: a working session or a child is never moved; due or missing checkpoints are refreshed first.
   The covered sessions continue without the state of the old VM: from the newest checkpoint the device has of them
   or of their task, else as a fresh conversation that reads the task's messages - the existing restore rules.
   A request covers nothing with a hub below `vm_upgrade_version` 2: such a hub cannot show the hold and its
   consequence before the member confirms.
5. **Visible.** The hold reason `checkpoint_impossible` (`session_id`, `since`) carries the optional `checkpoint_at`:
   the time of the checkpoint the session would continue from (the newest cached one of the session, else of its
   task; absent: it starts fresh), answered by the contexts area through the new optional retire hook
   `fallbacks(context, row, session_ids)`. Compatibility: the hub's desired state names `vm_upgrade_version: 2`
   (always); a runner sends the new reason and field only to such a hub and reports `checkpoint_missing` without
   `checkpoint_at` to an older one, which refuses unknown values. The web shows the hold in German and English
   (what is missing, that the VM stays, what a switch means), and the confirmation of "Switch now" lists the sessions
   that continue without the old VM's state, with their checkpoint time or "fresh". The CLI preview prints the same
   status (`upgrade.holds[].checkpoint_at`).
6. **Tests** (goal 4): units for the judgement with the new outcomes, the classification of export refusals, no
   renewed due state (`mark_due`, `mark_periodic`, `mark_vm_due`), the persisted counters across a new
   `GoldenSwitch` (a runner restart); integration with the runner's PostgreSQL: a stopped VM on an old golden with a
   never used paused session switches in the next pass and the session resumes on the new VM without checkpoint; a
   VM with a never savable session stays, is not started after a restart, and a request by hand moves it; the hub's
   `vm_switch_at` and `vm_upgrade_version`; web: the hold text and the confirmation.

### Consequences
- The VM with only a session that never worked moves in the first pass after the update.
- The VMs whose sessions worked but whose guest has no export route are started at most `MAX_REFRESHES` times once
  more (their `dropped` rows from before cannot tell why they were dropped); the first refused export makes each
  session `impossible`, and from then on the VM is neither started nor stopped for the switch, also after restarts.
  It stays on its golden, visibly, until a member switches it by hand.
- A session that is moved by hand loses what its old VM held since its last saved checkpoint (or everything when
  there is none); the confirmation says so before.
- Remaining risks: (a) a new task of a workgroup whose stopped VM is held by `checkpoint_impossible` still starts on
  that VM and its old guest (the package excludes changes to the choice of a VM, `_choose`; the hold is visible and a
  switch by hand ends it); its work cannot be checkpointed there either and becomes `checkpoint_impossible` itself.
  (b) A guest fault that the guest reports as `state_conflict` (no Claude session ID recorded, unreadable transcript
  directory) for a session that never had a checkpoint counts as `nothing` - the package's rule; such a session then
  continues fresh after a switch, like one whose first turn never completed.
