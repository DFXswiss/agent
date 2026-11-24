# KOR-39 – decisions (situation view "autonomous first")

Architecture: [ADR-064](../adr/064-situation-view-and-attention-categories.md). Concept: [Idee 32](../KONZEPT.md#k-idee-32),
[Idee 31](../KONZEPT.md#k-idee-31), rules [E-02](../KONZEPT.md#e-02), [E-04](../KONZEPT.md#e-04),
[E-05](../KONZEPT.md#e-05), [E-19](../KONZEPT.md#e-19).

1. **Categories at the hub, not in the client.** The hub derives one attention record per task from metadata it
   already holds; a client-side derivation would have to page through every task of every workgroup (E-19) and exist
   twice (web and agent tool, E-04).
2. **A separate projection table** `hub.task_attention` instead of columns on `hub.task`: operation writers would
   otherwise lock the task row and change the lock order of the tasks area. The attention row is locked first inside
   the refresh and is the last lock of every transaction.
3. **The agent asks with an operation** (`human.question`, `human.approval`) instead of a new event kind: the type is
   visible like every operation's type, the question is sealed in `operation.params`, audit and idempotency come for
   free. No new MCP tool.
4. **No content-derived signal** (design review): the first draft marked a turn whose last sentence ended with "?" as a
   question. That is one bit taken from content and unreliable; dropped. Instead Claude Code's own asking tool
   (`AskUserQuestion`), recognized by name, becomes a `human.question` operation.
5. **Questions count only in the main session's last turn** (design review): an agent that carries on after asking
   starts a new turn, so its earlier question no longer shows; a refused operation counts only when no later operation
   of the same type succeeded. A subagent cannot ask a person (`rights_denied`); it reports to its main agent.
6. **Ordering**: "Needs your decision" longest waiting first (nothing starves), the other groups newest first. A phase
   change inside "Running" does not move the entry (`since` changes only with the category).
7. **Notifications** (design review): one per occasion; workgroup-wide causes (no computer, computer unreachable, all
   busy) once per owner, workgroup and reason while unread; no second notification for a failed task (`task.failed`);
   no `task.idle` for a task that needs a decision or is stuck.
8. **Approvals are prompts** starting with `Approved.` or `Declined.` (English, the same in every locale), so the agent
   needs no new input channel and the web client and the agent tool send the same text.
9. **"Try again" only where it can help** (session, guest, computer, capacity); a refused action gets "Open task";
   an empty balance says that the operator sets it (E-14) and offers "Continue".
10. **Numbers**: migration `0011_task_attention`, E2E scenario `E2E-47` (numbers 41–46 are taken by parallel
    packages); the ADR number 064 avoids the numbers 058–062 used on parallel branches.

## Hub

- **Lock order of the refresh.** `agent_hub.attention.store.refresh` takes the global audit lock (re-entrant; nearly
  every writer already holds it through `append_events` / `append_audit`) before it locks the attention row. So the
  row stays "the last lock" in the sense of ADR-064 §4 even where a writer appends audit rows after
  `refresh_task` (closing a task, the outbox items after a session transition): no transaction ever waits for the
  audit lock while it holds an attention row, and the refresh can sit inside `refresh_task` / `close_task` without
  every caller having to call it last.
- **Refresh points.** `refresh_task` refreshes also for a closed task (it does not re-derive the task state there), so
  the subagent counters follow the sessions that stop after closing. Operation writers refresh at their end:
  registration, the outbox item `operation_transition`, the expiry job.
- **Clocks of "last turn".** An operation belongs to the main session's last turn when its `created_at` (the
  runner's `requested_at`) is at or after the device time `at` of the main session's newest `working` transition
  (both device clock), and when its hub decision time `decided_at` is after the newest `prompt` event's
  `received_at` (both hub clock). The SQL already drops refused operations superseded by a later successful one of the
  same type, so at most one operation reaches `derive`.
- **Refs only for occasions.** Only `needs_decision` and `blocked` records carry a ref: the operation (rule 3) or the
  main session (rules 2 and 6); `device_missing` / `device_unreachable` of a scheduling task name nothing. A
  `working`/`done` record has no ref, so a new running operation is no change worth a frame beyond the phase.
- **Counters.** `subagents_total` counts the sessions of depth > 0, `subagents_active` those not `stopped`/`failed`.
- **A new row** starts with `seq` 1 and `since` = the task's `state_at` (for a new task the creation, for a backfilled
  or lost row the last state change, so "Recently finished" keeps its real order).
- **Backfill announces nothing**: no frame and no `task.attention` notification for pre-migration tasks; batches of
  200 tasks per transaction (each holds the audit lock briefly), every 60 s, first run 5 s after the start.
- **Reasons in the schema** are a pattern (`attention_reason`), not an enum: the closed list lives in
  `contracts/enums/attention.json`, so a later package adds a reason without breaking older clients' validation.
- **Cursor**: `next_before` has the form of the task lists (`since` in microseconds and the task ID) and continues in
  the category's order (ascending for `needs_decision`). `mine` accepts `0` and `1`; `since` is a contract time.
- **Notifications**: the rule notifies only while the record still has the signalled occasion (`seq`), so an
  occasion that already passed (answered within the same second) sends nothing; `created_at` is the rule's hub time.
  The workgroup-wide dedupe looks for an unread `task.attention` of the owner with the same workgroup and
  `<category>:<reason>:wg` ref. `task.idle` is suppressed by reading the committed record (both signals are sent after
  the same commit).
- **Export**: `task_attention` is exported with the org (its `since`/`seq` are history, not derivable); there is no
  projection rebuilder for it (`rebuild --check` would always differ on `since`), the backfill derives lost rows.
- **Stream filter**: the `attention` frame is published with `wg_id=`; without it the stream drops it (it is listed
  with the row frames, whose rows name their workgroup), so a frame never reaches a non-reader.
