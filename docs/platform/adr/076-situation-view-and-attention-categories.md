# ADR-076: The situation view – every task in one attention category the hub derives from metadata

Status: Accepted (2026-11-24)

## Context
- The concept makes autonomous work the main purpose: "Das Dashboard priorisiert Agentenstatus, Fortschritt,
  Blockaden und erforderliche Entscheidungen" ([Idee 32](../KONZEPT.md#k-idee-32)); people, main agents and subagents
  are told apart and the dashboard shows responsibilities and relations ([Idee 31](../KONZEPT.md#k-idee-31)). The home
  of an organization was a prompt field with the recent tasks of one workgroup; the task overview (KOR-32,
  [E-21](../KONZEPT.md#e-21)) shows only task states. Nobody could see at a glance where a human is needed.
- Rules that bound the solution: there can be ten agents or a thousand ([E-02](../KONZEPT.md#e-02)); everything works
  without a browser ([E-04](../KONZEPT.md#e-04)); a missing precondition is started, not explained
  ([E-05](../KONZEPT.md#e-05)); no view may fail on the amount of data ([E-19](../KONZEPT.md#e-19)); the hub never sees
  content ([§5 Punkt 2](../KONZEPT.md#k-5-b2), ADR-003).
- The hub already holds, in plaintext and for other reasons (`04-crypto.md` §7): task state and reason, session
  states, reasons and the session tree, operation types, states and error codes, the event headers (kind, task,
  receive time) and the device assignments of the workgroups. Everything the situation view needs to sort a task is a
  function of these – except the agent's own statement that it waits for a person, and the question itself.

## Decision

### 1. One attention record per task, derived by the hub
- Table `hub.task_attention` (one row per task, migration `0021_task_attention`): `category`, `reason`, `ref_kind` +
  `ref_id` (the object the next action refers to), `since` (when the task entered this category), `seq` (counts the
  occasions: +1 whenever the category changes, or the reason or ref inside `needs_decision` / `blocked`; a phase change
  inside `working` changes neither `seq` nor `since`, so the lists do not reshuffle), `subagents_total`,
  `subagents_active` and `updated_at`. It is a projection: a pure function `derive` of the metadata in §2 computes
  it, nothing else writes it, and a lost row is rebuilt.
- Categories and reasons are closed lists in the contracts (`contracts/enums/attention.json`), extensible by adding a
  row (a later package adds its reason code and its mapping rule; clients show an unknown reason with its category's
  generic text).

| Category | Group in the view | Reasons (plain words in the view) |
|---|---|---|
| `needs_decision` | "Needs your decision" (longest waiting first) | `question` "The agent has a question", `approval` "The agent asks for your approval", `connection_missing` "GitHub is not connected", `credit_empty` "The balance is used up", `device_missing` "No computer is assigned to this workgroup" |
| `blocked` | "Stuck" (newest first) | `session_failed` "The agent stopped with an error", `guest_error` "The agent's computer had a problem", `device_unreachable` "The computer is not reachable", `model_access_missing` "No access to an AI model", `no_capacity` "All computers are busy", `operation_refused` "An action was refused", `limit_reached` "A limit of the platform was reached" (ADR-088) |
| `working` | "Running" (newest first) | the phase: `queued` "Waiting for a computer", `starting` "Starting", `working` "Working", `operation` "Running an action", `subagents` "Helpers are working" |
| `done` | "Recently finished" (newest first) | `idle` "Finished – waiting for you to close or continue", `closed` "Closed" |

### 2. Derivation (deterministic, first rule that applies)
Inputs: the task row, its main session (newest of depth 0) with the end of its previous turn (its last `idle` before
its newest `working`; the guest may request an operation a moment before it reports `working`, so the start of the
turn is no reliable bound), the time of the task's newest `prompt` event (the last human input), the operations of
the task, whether its workgroup has an active assigned device, and its sessions. "Last turn" = operations of the main
session created after the end of its previous turn and decided after the last human input.

1. Task `closed` → `done/closed`.
2. Task `failed` → by the main session's reason: `budget_exceeded`, `credit_exhausted` (KOR-31) → `needs_decision/credit_empty`; `unassigned` →
   `needs_decision/device_missing`; `model_access_unavailable` → `blocked/model_access_missing`; `device_offline`,
   `device_shutdown` → `blocked/device_unreachable`; `vm_start_failed`, `guest_unreachable`, `sandbox_failed` →
   `blocked/guest_error`; `capacity_exhausted` → `blocked/no_capacity`; every other reason → `blocked/session_failed`
   (ref: the session).
3. The main session is not `working` (it ended its turn; the task may still be `working` because subagents run) and
   the newest operation of its last turn that matters is
   a. a `human.question` / `human.approval` that `succeeded` → `needs_decision/question` / `needs_decision/approval`
      (ref: the operation);
   b. an operation that ended `rejected`, `denied`, `failed` or `expired` with a blocking error code, and no later
      operation of the same type of the task `succeeded`: `github_not_connected` → `needs_decision/connection_missing`;
      `budget_exceeded`, `credit_exhausted` → `needs_decision/credit_empty`; `rights_denied`, `repo_not_accessible`, `repo_not_allowed`,
      `branch_protected`, `secret_detected`, `model_not_allowed` → `blocked/operation_refused`, `limit_exceeded` → `blocked/limit_reached` (ADR-088) (ref:
      the operation). Other error codes (`params_invalid`, `no_changes`, `non_fast_forward`, `timeout`, …) are the
      agent's own business and do not matter.
   The newest of a. and b. decides. An agent that carries on after asking (a subagent's finish wakes it) starts a new
   turn, so its earlier question no longer counts.
4. Task `scheduling` → by its waiting reason (03-api.md §4.6.2, KOR-41): `no_device`, `platform_access_required` →
   `needs_decision/device_missing`; `device_offline`, `placement_offline` → `blocked/device_unreachable`;
   `credit_exhausted` → `needs_decision/credit_empty`; `capacity_exhausted`, `wg_vm_limit`, `placement_full`,
   `keys_pending` (a key of an organization device is still on its way) → `working/queued`; without a known reason:
   no active device assigned to the workgroup → `needs_decision/device_missing`, otherwise `working/queued`.
5. Task `starting` → `working/starting`. Task `working` → an operation of the task `accepted` or `executing` →
   `working/operation`; no session of depth 0 `working` but a subagent `working` → `working/subagents`; else
   `working/working`.
6. Task `idle`: the main session's reason `budget_exceeded` or `credit_exhausted` (a credit stop) → `needs_decision/credit_empty`; otherwise
   `done/idle`.

A follow-up prompt is a new human input: everything before it no longer counts, so answering a question moves the
task out of `needs_decision` in the same transaction that stores the prompt.

### 3. The agent reports by itself – and the platform notices without it
- Two operation types (03-api.md §9): `human.question` and `human.approval`, params `{question}` (1–4000 characters).
  The question travels sealed in the `operation.params` event like every operation's parameters; the hub sees the type
  only. The runner checks the length and ends the operation `succeeded` with `{}` at once; there is no duplicate window
  (idempotency key only). Only a main session may ask a person: a subagent's request is `denied` with
  `rights_denied` (a subagent reports to its main agent, [Idee 31](../KONZEPT.md#k-idee-31)). The harness rule
  (05-runner.md §8) tells the agent: when only a person can decide, ask with `request_operation` of one of these types
  and end the turn; do not ask in plain text and wait.
- Claude Code's own way of asking, the tool `AskUserQuestion`, would leave the session waiting in its terminal forever.
  The guest's PreToolUse policy recognizes the tool by its name (a formal check, no content is inspected) and turns
  the call into a `human.question` operation of the session (the question and its options sealed as usual), then
  denies the tool with a fixed sentence telling the agent that the person was asked and that it should end its turn.
- No content-derived signal goes to the hub: a turn that simply ends with a question in plain text is `done/idle`
  (the person sees it as "Finished – waiting for you" and is notified by `task.idle` as before). Everything else the
  platform notices without the agent's help follows from §2 alone: refused operations, failed sessions and guests, a
  workgroup without a device, an empty balance.

### 4. Consistency and volume
- Every writer that changes an input refreshes the record at the end of its transaction:
  `agent_hub.sessions.taskstate.refresh_task` and `close_task` (session transitions, prompts, scheduling) and the
  writers of operation states. The refresh first locks the task's attention row (`FOR UPDATE`, inserting it when
  missing) and only then reads its inputs, so under READ COMMITTED two concurrent writers serialize on that row and the
  later one derives from the earlier one's committed state. The row is always the last lock a transaction takes (no
  other lock is requested after it), so it adds no deadlock cycle to the lock order of the tasks area.
- A job (`attention.backfill`) derives the records of tasks that have none (tasks from before the migration, a lost
  row) in batches; it is idempotent.
- Reading is paged at the hub: `GET /v1/orgs/{org}/attention?category=…` returns at most 100 rows per page with a cursor,
  ordered by `since ASC, task_id ASC` for `needs_decision` (longest waiting first) and `since DESC, task_id DESC`
  otherwise, over the workgroups the caller reads (index on `org_id, category, since,
  task_id`); `GET /v1/orgs/{org}/attention/summary` returns the counters. The web client renders windowed lists
  (the building blocks of the chat bundling, HOT-05) and loads a subagent tree only when an entry is opened (the tree of
  `GET /v1/tasks/{task}`, at most 100 sessions, the rest counted). A volume test covers thousands of tasks and deep
  subagent trees.
- Stream frames `attention` are sent only when the record changed (category, reason, ref, seq or the subagent
  counters), never for a refresh that changed nothing; the client applies them in batches (at most one store update
  per 16 ms, HOT-05) and refetches the counters at most once per second.

### 5. Notifications: one per occasion
- New notification kind `task.attention` to the task owner (the responsible person), while the owner reads the
  workgroup, when a task enters `needs_decision` or `blocked`: once per occasion (`ref` = `<category>:<reason>:<seq>`).
  Causes that concern a whole workgroup (`device_missing`, `device_unreachable`, `no_capacity`) notify each owner once
  per workgroup and reason (`ref` = `<category>:<reason>:wg`) while an earlier such notification of that owner is
  unread, so one missing computer never sends fifty notifications. A further occasion of the same task notifies only once the previous
  `task.attention` of that task was read, so an agent that asks again and again does not flood its owner. A `blocked` record that comes from a failed task is
  already notified as `task.failed` and gets no second notification, and a failed task whose record needs a decision
  (empty balance, no computer) gets `task.attention` instead of `task.failed`; `task.idle` is not sent for a task whose record
  is `needs_decision` or `blocked`. Repeated derivations of the same occasion never notify twice.

### 6. Who sees what
- Rights: `org:member`, and only tasks of the workgroups the caller reads (the rule of `wg:read`); a workgroup the user
  cannot read never appears, not even in a counter. "Waiting for me" counts the `needs_decision` tasks the caller owns.
- New plaintext at the hub (added to `04-crypto.md` §7): the attention record (category, reason code, ref, times,
  counters) – a function of metadata the hub already holds – and the two operation types. Titles, questions and answers stay sealed and are opened in the client.

### 7. Agent access (E-04)
- The same situation through the API and `agent-platform`: `agent-platform status --attention [--org ORG]` (the
  counters and every `needs_decision` and `blocked` entry with title, workgroup, owner, reason and – for `question` /
  `approval` – the question, opened and verified locally) and `agent-platform task list --org ORG --state
  needs-decision|blocked|working|done [--mine]` (one category, all pages up to `--limit`); an answer is
  `agent-platform task prompt --task T --text …`, an approval `task prompt --task T --approve|--decline [--text …]`
  (the same prompt text as the web client, §8). Rows in `agent-parity.md`; the harness tool is the operation type
  itself (no new MCP tool).

### 8. Answers and approvals are ordinary prompts
- An answer is a follow-up prompt with the person's text. An approval is a follow-up prompt whose first line is
  exactly `Approved.` or `Declined.` (English, so the agent reads it the same in every locale), followed by a blank
  line and the person's comment when one was given. The agent continues in the same session (§4.6.3 of 03-api.md).
- Actions offered in the view (06-web.md §4.0) are the next step, never only an explanation (E-05): connect GitHub,
  assign a computer (admins), open the model settings; "Try again" only where a new attempt can help (`session_failed`,
  `guest_error`, `device_unreachable`, `no_capacity`); `credit_empty` says that the operator sets the balance
  ([E-14](../KONZEPT.md#e-14)) and offers "Continue" once it is set.

## Consequences
- The organization's home is the situation view; a compact prompt field with the workgroup selector stays at its top
  (reachable, no longer the main content) and the task overview (KOR-32) stays; both use the same row building blocks.
- Later packages add reasons (KOR-31 credit stop, KOR-41 "waits for a device", KOR-25 GitHub start) by a row in the
  enum and a rule in `derive`; their own areas stay theirs.
- Rollout order: hub first (migration, endpoints, backfill job), then the runner (operation types), then the golden
  (harness rule, `AskUserQuestion` turned into `human.question`). An old runner refuses the new types (`unknown_type`), an old golden never
  requests them; an old web client ignores the new endpoints and frames.

## Alternatives considered
- **Compute the categories in the client from task lists**: every client would page through every task of every
  workgroup (fails E-19) and the agent tool would need a second implementation.
- **A new event kind for the question**: would need a plaintext reason in an event header; an operation already has a
  visible type, an audit trail and a sealed parameter event.
- **Columns on `hub.task`**: the operation writers would have to lock the task row and change the lock order of the
  tasks area; a separate leaf row avoids that.
