# KOR-39 – decisions (situation view "autonomous first")

Architecture: [ADR-076](../adr/076-situation-view-and-attention-categories.md). Concept: [Idee 32](../KONZEPT.md#k-idee-32),
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
10. **Numbers** on the united tree: ADR 076, hub migration `0021_task_attention`, E2E scenario `E2E-58` (renumbered
    at each rebase as packages merged before this one).

## Hub

- **Lock order of the refresh.** `agent_hub.attention.store.refresh` takes the global audit lock (re-entrant; nearly
  every writer already holds it through `append_events` / `append_audit`) before it locks the attention row. So the
  row stays "the last lock" in the sense of ADR-076 §4 even where a writer appends audit rows after
  `refresh_task` (closing a task, the outbox items after a session transition): no transaction ever waits for the
  audit lock while it holds an attention row, and the refresh can sit inside `refresh_task` / `close_task` without
  every caller having to call it last.
- **Refresh points.** `refresh_task` refreshes also for a closed task (it does not re-derive the task state there), so
  the subagent counters follow the sessions that stop after closing. Operation writers refresh at their end:
  registration, the outbox item `operation_transition`, the expiry job.
- **Clocks of "last turn".** An operation belongs to the main session's last turn when its `created_at` (the
  runner's `requested_at`) is after the device time `at` of the main session's last `idle` before its newest
  `working` transition (both device clock; the start of the first turn when there is none), and when its hub decision time `decided_at` is after the newest `prompt` event's
  `received_at` (both hub clock). The SQL already drops refused operations superseded by a later successful one of the
  same type, so at most one operation reaches `derive`.
- **Refs only for occasions.** Only `needs_decision` and `blocked` records carry a ref: the operation (rule 3) or the
  main session (rules 2 and 6); `device_missing` / `device_unreachable` of a scheduling task name nothing. A
  `working`/`done` record has no ref, so a new running operation is no change worth a frame beyond the phase.
- **Counters.** `subagents_total` counts the sessions of depth > 0, `subagents_active` those not `stopped`/`failed`.
- **A new row** starts with `seq` 1 and `since` = the task's `state_at` (for a new task the creation, for a backfilled
  or lost row the last state change, so "Recently finished" keeps its real order).
- **Backfill announces nothing**: no frame and no `task.attention` notification for pre-migration tasks; batches of
  20 tasks per transaction (each holds the audit lock briefly), every 60 s, first run 5 s after the start. The job
  `attention.recheck` re-derives the tasks waiting for a device (their record depends on device assignment and presence,
  which change without a write to the task), one short transaction per task (review).
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

## Web

- **Composer stays on top** (coordinator change for the existing full-chain specs): `/o/:org` keeps the compact
  composer (`home.context`, `home.org`, `home.wg`, `composer.*`) above the counters and groups instead of a "New task"
  toggle; the chips are open while every group answered empty (the organization shows no task), otherwise behind
  "Suggestions" (`home.suggestions`). `home.all-tasks` moved into the header of "Recently finished"; `home.recent`
  and `attention.new-task` do not exist.
- **Store**: one slice `attention` (records by task, groups per org and category, counters per org). A changed record
  leaves every group of another category and enters its own loaded group only when it sorts before that group's last
  loaded record (or the group is complete), so paging never shows an entry twice. Older records (by `updated_at`,
  then `seq`) are ignored. A frame's org is the org a page brought the task in, else the active org of the channel.
- **Batching**: `attention` frames are buffered per store and written once per 16 ms; each batch bumps `changes`,
  which the counters follow through `refreshSummary` (at most one request per second per org, a burst ends with one
  more request). Frames buffered before a sign-out are dropped.
- **Window**: 50 entries per group in the DOM, moved by 25 ("Show later entries" / the end of the list coming into view,
  which loads the hub's next page when fewer than 25 loaded entries are left; "Show earlier entries"). Entries are
  memoized; the window keeps its place when its first entry leaves the group. No virtualizer (HOT-05 reasons).
- **Entry** = `TaskItem` with `children`: the row is then not one link; the attention line (reason, main agent state
  once its session is in the store, time in the group, helpers for "Running") follows, then the actions, "Open task"
  and the tree toggle. The task state badge stays the row's state; the main agent's own state is shown when known
  (from the tree or the task page), because the attention row carries no session state.
- **Unknown task rows** (a frame of a task created elsewhere) are loaded once with `GET /v1/tasks/{task}`; an entry
  whose workgroup the verified log does not let the user read is not rendered (second check after the hub).
- **Question**: read from the verified `operation.params` of `ref_id` (operation row and event pulled with
  `ref=`); shown only when the event's type is the record's asking type and the hub row (when loaded) names the same
  type; otherwise "cannot be verified (code)".
- **Actions**: answers, approvals and "Continue" go through the chat's `sendPrompt` (the sealed prompt is kept for a
  retry); "Continue" sends `Continue.`; "Try again" is the task page's `retryPrompt` (the newest prompt again).
  "Connect GitHub" and "Open model settings" lead to `/settings` (the sections live there); "Assign a computer" leads
  admins, and every writer of a private organization, to `/o/:org/devices`, others see who can do it. Read-only users
  see the entry with "Open task" and the tree toggle only (navigation, not controls).
- **Badge**: `NavEntry` got an optional `Badge` component (Home registers it in its own `nav.ts`); the sidebar/row and
  the bottom bar render it, so no central list changed. The badge shows `needs_decision_mine` (hidden at 0, `99+`).
- **Notifications**: `task.attention` texts are built from `ref` (`<category>:<reason>:…`) with the reason's plain
  words (generic text for an unknown reason, a neutral text for a malformed ref); a click leads to `/o/:org`.
- **Operation card**: `human.question` / `human.approval` are known types in the chat ("Ask a person", "Ask for
  approval") with the question as subject.
- **E2E**: `e2e/devices/attention.spec.ts` runs in `devices` and both phone projects (added to `MOBILE_DEVICE_SPECS`
  like KOR-32); `e2e/devices/attention-volume.spec.ts` (1 500 tasks waiting for an offline computer, created through
  the API paced below the session rate limit) runs on desktop and, inside the file, with the Pixel 7 profile.

## Runner, guest, agent tool

- **A subagent's question is denied by the hub, the runner only guards.** The runner cannot produce the state
  `denied` itself (a local refusal is registered as `rejected`, `03-api.md` §8.3), so it registers `human.question` /
  `human.approval` of a subagent like any request and the hub denies it at check H4 (`rights_denied`). Should a hub
  accept one anyway (the hub is not trusted), the executor fails it `rights_denied`; it never ends `succeeded`.
- **The executor does nothing.** The question already travels sealed in the `operation.params` event of the
  registration; the result is `{}` at once and no further event is written. The limit (4000 characters) is the rule
  constant `QUESTION_LIMITS` beside the other limits of `operations/rules.py`.
- **Rule 1a is decided in the control process.** The API process builds the question from the hook's tool input
  (`policy.question_text`: each question with its options as `- label: description` lines, questions separated by a
  blank line, NUL removed, cut to 4000 characters) and passes it with the `hook.pre_tool_use` op; the control process
  knows whether the session is a main session (depth 0 of the identity stored when the session was created,
  `SessionManager.depth`), creates the pending `operation.request` exactly as `request_operation` does (new
  `operation_id`, `local_check:"passed"`, no idempotency key) and denies the tool. Nobody waits for the answer of that
  pending call; the runner seals, registers and executes it as usual.
- **Fallbacks go to rule 2.** A subagent's call, a session whose depth cannot be read and a call whose input holds no
  question text are denied with the unchanged rule-2 sentence; no operation is requested.
- **The 1a deny is journaled as rule 1.** The journal and event schemas allow rules 1-7, the audit `hook.denied`
  only 2-6; the runner audits denies of rules 2-6 only, so the question leaves no `hook.denied` row (the operation
  is its record).
- **Contract copy.** The new deny sentence (`question_passed`, rule 1) is added to
  `contracts/mcp/hook-deny-reasons.json` and to the `deny_reason` enum of `contracts/mcp/objects.json`; agent-core
  mirrors both.
- **The harness tool stays `request_operation`.** `RULES.md` gets both types with a parameter example and a section
  "When only a person can decide" (ask, end the turn, `Approved.` / `Declined.`, a subagent reports to its main
  agent, never `AskUserQuestion`); the generated tool list keeps its single `task.set_title` example.
- **Agent tool shapes.** `status --attention` without `--org` covers every organization of the user, each with the
  counters and every `needs_decision` and `blocked` entry (at most 1000 per group); `task list --org` requires
  `--state` (one category), defaults to 100 entries, at most 1000; `--mine` and `--limit` are refused with `--wg`.
  An entry carries the hub's record plus the verified title, workgroup and owner names; for `question` / `approval`
  the question, or `null` when it cannot be opened or does not name the record's operation and type. Items that do
  not fit the API shape are left out and counted (`unverified`).
- **Approval text.** `task prompt --approve|--decline [--text]` sends `Approved.` / `Declined.`, plus a blank line
  and the comment when it is not blank; `--text` became optional only for these two.
- **ADR numbering test.** ADR-076 leaves a gap (058-063 are not on this branch; parallel packages use 058-062), so
  the documentation test now requires unique numbers starting at 1 instead of a gap-free sequence.

## Review of the united branch (subagent, fresh context)

No high finding. Fixed: (1) `updated_at` of a record grows strictly per row (`GREATEST(now, updated_at + 1 µs)`), so the
web client's "newest record wins" never drops a later write; (2) `attention.recheck` refreshes one task per short
transaction and the backfill batches are 20 tasks (both hold the global audit lock while refreshing); (3) a further
`task.attention` occasion of a task notifies only once the previous one of that task was read (an agent asking again
and again cannot flood its owner; the asking operations themselves stay unlimited, E-11); (4) the web client shows a
question only with the hub's operation row loaded: its device must be the author of the sealed parameters and it must
be an asking operation of the record's task; (5) a failed task whose record needs a decision gets `task.attention`
instead of `task.failed`; (7) a record created late for an older task announces nothing; the lock-order wording of `store.py` was corrected.
Kept, with reasons: (8) the refresh is one call at the end of the few writers that change an input (task derivation,
operation states) instead of a new discovered hook: it must run inside their transaction in a fixed place of the
lock order, which an observer list would hide, and the parallel packages edit the same writers in the same way;
`e2e/platform/traceability.json` gets the row of the new scenario E2E-58 as KOR-34 and KOR-37 did for theirs.
(9) sorting a group on a changed record is bounded by the loaded entries (pages of 50, the window of 50 in the DOM);
the volume spec (1 500 entries) stays within its limits, so the binary insert is left as a follow-up. Observing the
end marker again after every window move was tried and reverted: with the marker in view it moved the window forward
again right after "Show earlier entries" (volume spec); "Show later entries" stays the way on, the marker loads once
per reach of the end.

## After the rebase onto HOT-09

The full-chain scenario showed that the guest now reports `working` a moment after the agent's first operation of a
turn (the first prompt waits for the platform tools, ADR of HOT-09): the question was requested 36 ms before the
`working` transition and fell out of the "last turn". The turn is now bounded by the end of the previous turn (its last
`idle` before the newest `working`) instead of the start of the current one; an agent that carries on after asking
still starts a new turn after an `idle`, so its earlier question no longer counts.

## Gate hygiene (E-19)

Two tests that read their own stderr (`hub/db/test_cli.py`, `monitoring/test_patch.py`) failed in several gate runs,
also of other packages: a transcript capture thread that a guest session test left running read its closed journal
later and wrote "transcript capture failed" into the stderr of whichever test ran next in that worker. The session
fixture now ends every capture of its context at teardown (`stop_captures` in `tests/platform/guest/sessions/
conftest.py`), so no test leaves a capture thread behind.
