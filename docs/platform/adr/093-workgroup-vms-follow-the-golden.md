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
| Large workspaces | only up to `AGENT_RUNNER_CONTEXT_WORKSPACE_MIB` (4096) and `files.MAX_ENTRIES` (100 000): above that the workspace is left out (`workspace: omitted_too_large`) | `contexts._workspace`, `context.json` |
| Links leaving the workspace, devices, FIFOs, files of another owner, unreadable entries, protected names | left out and counted (`workspace_omitted` > 0, restore state `partial`) | `_ContextExport.skip`, `archive.Context.workspace_state` |
| Background processes of the session | no - but they do not survive today either: a paused session runs no agent, and every idle VM is stopped after `VM_IDLE_SECONDS` | `vm.lifecycle.idle_pass` |
| The session's home outside `.claude` (caches, tools installed there) | no - tool state; the new golden brings the tools | `contexts` module docstring |
| Work VMs (test, browser) of the session | not needed: separate Lima instances, not tied to the workgroup VM; the mirror is synchronised again by its manifest | `workvm.manager`, 13-work-vm.md |
| Child sessions (subagents) | no checkpoint; they live in their parent's VM and are never moved | `prompts.resume` (child sessions), ADR-088 |
| Whether a checkpoint was complete | the log line `checkpoint_taken … workspace=… omitted=…`, not stored | `checkpoint.Checkpointer._checkpoint` |

The reason "unsaved files of paused sessions would be lost" no longer holds for main sessions whose newest checkpoint
is complete; it still holds for an incomplete workspace (too large, partly left out), a checkpoint that is due or
missing, and for child sessions. That is the rule below.

## Decision

### 1. When a VM is ready to move
A live workgroup VM whose `golden_version` is not the newest `ready` golden of the device is **outdated**. It is
**ready** when every session on it that is not `stopped` or `failed` is a main session in state `suspended` whose
checkpoint is complete:

- no checkpoint of it is due (`runner.context_session.due_at` NULL),
- a checkpoint was taken (`archive_sha256` set) and its newest object is in the local cache (`runner.context_object`,
  any state: the local copy is complete; the newest object of every task is never evicted),
- the workspace of that checkpoint was `full` or `empty` (new column `runner.context_session.workspace_state`, written
  with every taken or identical checkpoint, migration `0034_golden_switch`).

Otherwise it is **held**, with one entry per cause (`holds`, at most 16, sessions in ID order):

| `reason` | Who holds | Ends when |
|---|---|---|
| `session_active` | a session that is not paused (`session_id`) | it is paused, stopped or failed |
| `subagent` | a child session that is not stopped or failed (`session_id`) | it ends |
| `checkpoint_due` | a paused session whose checkpoint is still due (`session_id`) | it is taken |
| `checkpoint_missing` | a paused session without checkpoint or without its local object (`session_id`) | a checkpoint is taken |
| `workspace_incomplete` | a paused session whose newest checkpoint left (part of) the workspace out (`session_id`) | a later complete checkpoint |
| `capacity` | the device has no free slot for the new VM without stopping another workgroup's VM | a slot is free |
| `switch_failed` | the last attempt could not make the new VM ready | the next attempt (5 min, doubling, at most 1 h) |

`since` of an entry is the time the runner first saw it (kept in memory; a runner restart starts it again).
A paused session whose completeness is unknown (checkpointed before this migration: `workspace_state` NULL) gets a
fresh checkpoint: on a running VM at once (`retire.flush`), a stopped VM is started once for it when a slot is free
without stopping another workgroup's VM (the export is fast when nothing changed: `identical`).

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
resume chooses a VM of the workgroup (normally the new one) and restores the session there from its checkpoint
(ADR-072 section 7). The restore note and the audit say so (section 4). A session that has no checkpoint never gets
here: its VM stays as it is.

The pass runs in task `vm-pool` after every refill (every 60 s and after every assignment) and after a desired state
that asks for a switch (section 5). The existing replacement at a start (05-runner.md section 3.3 level 3: a stopped
outdated VM with room) follows the same rule: it replaces a VM whose suspended sessions are all ready, and creates
the new VM before it deletes the old one.

### 3. Visibility
`vm_state` (outbox) gains the optional object `upgrade` and the optional `replaced_by` (contracts, additive):

```
upgrade: {golden_version, outdated_since, state: "waiting" | "switching", holds: [{reason, session_id?, since}],
          sessions: [session_id]}   // sessions: the paused main sessions that move with the VM
replaced_by: vm_id                  // on the `deleted` report of a switched VM
```

The runner reports an outdated VM's status whenever it changes (`upgrade: null` once current). The hub stores it
(`hub.vm.upgrade`, `hub.vm.replaced_by`, migration `0035_golden_switch`) and shows it to `wg:read` in
`GET /v1/workgroups/{wg}` (`devices[].vms[]` gain `golden_version` and `upgrade`). When a switched VM is reported
deleted, a workgroup default (`wg_placement.vm_id`) or task choice (`task.placement_vm_id`) naming it names the new VM.
The web shows on the workgroup screen per outdated VM "New version waits" with the holds ("Session … is working since
…") or "moves when the sessions are paused", and the CLI `agent-platform wg vm-switch --wg W --vm V --preview`
prints the same.

### 4. What the user notices
The restore note of the moved session (`restored_line`, place `golden`): "This is your own earlier conversation of
this task, continued from its checkpoint of … on the new version of the workgroup VM; your files were carried over."
The audit `session.context_restored` gains the optional `moved: "golden"`; the hub stores it on the session as
`context_reason = golden_switch` and the task shows one line: "Continued on the new version of the workgroup VM -
files carried over."

### 5. By hand
`POST /v1/workgroups/{wg}/vms/{vm}/switch` (browser or agent key, `wg:read` and `org:admin` of the workgroup's
organization - the member with administration rights) asks the device to switch now: `404 not_found` when the VM is
no live VM of the workgroup, `409 vm_current` when its last report names no `upgrade`. It stores
`switch_requested_at/by`, writes the audit `vm.switch_requested {vm_id}` and sends the device the hint `reconcile`;
the answer is the preview `{vm_id, requested_at, upgrade}`. The desired state names the requested VMs of the device
(`vm_switch: [vm_id]`, new section); the runner's reconcile step wakes the pass, which takes requested VMs first. The
same rules apply - a request never moves a VM with a running session or without complete checkpoints (Abgrenzung of
KOR-70); it only saves the wait. Command: `agent-platform wg vm-switch --wg W --vm V` (without `--preview`); button
"Switch now" on the workgroup screen with the preview as confirmation.

### 6. What does not change
No VM with a running session is ever switched; nothing is deleted before the new VM runs; no new protective check
(E-25). Child sessions keep their VM. The hub learns no content (only IDs, reasons and times).

## Consequences
- A used workgroup reaches the newest golden at the latest one pass after all its sessions are paused with complete
  checkpoints; one that keeps a session working stays and says why.
- A session paused with a workspace above 4 GiB keeps its VM on the old golden until its workspace is checkpointed
  completely or the session ends (visible as `workspace_incomplete`).
- E2E-69 (T1): a VM on an old golden with two paused sessions whose files changed is switched; both continue on the
  new VM with their files.
