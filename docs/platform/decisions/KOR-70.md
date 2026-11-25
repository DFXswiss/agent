# KOR-70 – Decisions

Workgroup VMs move to the current golden by themselves; paused sessions move with them. Design: ADR-093.

1. **Evidence first.** The code was checked before anything was built (table in ADR-093 "What a checkpoint carries"):
   a checkpoint carries the conversation and the whole workspace as files - never committed files too, because a
   checkout is a plain tree without `.git` and commits are platform operations. Not carried: the workspace above
   4 GiB or 100 000 entries, entries left out (links leaving the workspace, special or foreign files), background
   processes (they end with every pause anyway), the session home outside the conversation's files, child sessions
   and the orchestrator session. A request that ended without a checkpoint (`dropped`) left the older checkpoint
   looking current; the new `checkpoint_state` closes that gap.
2. **Completeness is stored, not inferred.** `runner.context_session.checkpoint_state` (migration
   `0034_golden_switch`) is written with every taken or identical checkpoint and set `dropped` when a request ended
   without one. Rows from before the migration are NULL: the switch takes a fresh checkpoint first (a running VM is
   stopped with its final checkpoints; a stopped VM is started for it with a backoff).
3. **The VM area asks, the contexts area answers.** `RetireHook` gains an optional `checkpoints` judgement
   (`vm/extensions.py`); a paused session no hook vouches for holds its VM. Without an enrolled device nothing moves.
4. **Order of the switch:** final checkpoints (stop), judge again, the new VM running, only then delete the old one.
   The existing replacement at a start follows the same rule and order (before: delete first, then clone).
5. **Never stop another workgroup's VM for a switch** (`evict=False`): the switch is background work; `capacity` is
   shown instead.
6. **Children and the orchestrator hold.** Moving a paused child would lose its own workspace (no checkpoint, and the
   cache keeps the newest object per task, so a child's checkpoint would push out its parent's). Kept as a visible
   hold (`subagent`) and named as a remaining risk; a later package can checkpoint children. The orchestrator never
   pauses: `session_active`. No new protective check (E-25): both follow from "never without a checkpoint".
7. **Times survive restarts.** `since` of a hold is persisted (`runner.golden_switch_hold`) - "wer hält fest, seit
   wann" must not reset with a runner restart.
8. **Compatibility with an older hub.** `vm_state` and audit details are closed objects at the hub
   (`additionalProperties: false`). The hub's desired state always names `vm_switch`; only then the runner sends
   `upgrade`, `replaced_by` and `moved`. The switch itself works with any hub.
9. **No new error code for "already current".** `POST …/switch` answers `requested: false` with `upgrade: null`
   (simpler for agents, E-26) instead of a new `409`.
10. **Rights.** The route is declared `wg:read` (the router's single right) and checks `org:admin` of the workgroup's
    organization inside (`403 forbidden`); "Mitglied mit Verwaltungsrecht" = organization admin, the only
    administration role.
11. **A member's VM choice follows the switch.** `wg_placement.vm_id` and `task.placement_vm_id` naming the replaced
    VM are moved to its successor when the hub stores `replaced_by` - not needed for correctness, but the choice
    stays a choice.
12. **What the user notices** is one line: the restore note (to the model) and `context_reason = golden_switch` on the
    session (the task shows "Continued on the new version of the workgroup VM – files carried over.").
13. **T2 not run** (no second test device in operation, INF-05): the switch is local to one device; nothing of it
    crosses devices.
