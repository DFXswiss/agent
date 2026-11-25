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
upgrade: {golden_version, outdated_since, state: "waiting" | "switching", holds: [{reason, session_id?, since}],
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
