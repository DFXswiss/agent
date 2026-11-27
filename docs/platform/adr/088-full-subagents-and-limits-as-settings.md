# ADR-088: Full subagents and limits as settings

Status: Accepted (2026-11-25) – supersedes [ADR-017](017-subagent-limits.md) (fixed limits, one-shot children) and the
one-shot end, the 30-minute runtime and the single ping of [ADR-038](038-runner-subagent-control.md); supersedes
decision 11 of `decisions/KOR-40.md` ("subagents stay one-shot").

Implements [Idee 26](../KONZEPT.md#k-idee-26) ("Die Plattform startet eine vollwertige Claude-Code-Session … Der
Subagent berichtet an seinen übergeordneten Agenten"), interpretations [A1](../KONZEPT.md#a-1) (a subagent may do what
its main agent may), [A10](../KONZEPT.md#a-10) (subagents get follow-up instructions and keep running),
[A5](../KONZEPT.md#a-5) and [A14](../KONZEPT.md#a-14) (fixed limits become settings with generous defaults, a clear
message when one is reached, reporting caps never make monitoring blind), rules [E-02](../KONZEPT.md#e-02),
[E-04](../KONZEPT.md#e-04), [E-11](../KONZEPT.md#e-11) and [E-19](../KONZEPT.md#e-19).

## Context
- A child ended at its first `Stop` (ADR-017, ADR-038); no tool could send it a second instruction, it was failed
  after 30 minutes, and its parent got one ping. "Vollwertig" (Idee 26), A1 and A10 ask for a child that keeps its
  context and continues when its main agent asks it to.
- The subagent limits (depth 2, 3 running, 10 per task, 30 min) were constants in five places (runner harness,
  runner subagent rules, runner operation rules, hub operation limits, hub `/v1/config`) and in three contracts. The
  guest reported at most 64 sessions per VM (health and metrics; the runner refused a metrics answer above 64) and the
  heartbeat at most 500 sessions and 256 VMs per device: above that, monitoring silently lost sessions.

## Decision

### 1. A child lives until its main agent ends it
- **Start** as before: `subagent.request {purpose, prompt, model_id?, github_identity?}`, hub check H4, the child is a
  session of the same task on the parent's VM with its own sandbox, harness, hooks, sync, audit, monitoring and live
  view. Its first instruction is the request (instruction ID = the operation ID).
- **Turns.** When a turn of the child ends (`idle`), the child **keeps running**: its last assistant message becomes
  the result of that instruction, the parent gets a `subagent.finished` ping **per turn** (ping ID stable per child
  and instruction, `ref` = the child), and the child waits for the next instruction. Like a main session, an idle
  child is suspended after the device's idle time (`AGENT_RUNNER_SESSION_IDLE_SECONDS`, the existing idle rule) and
  resumed with its conversation by the next instruction. A turn that ends with `StopFailure` ends the child `failed`
  `agent_exited` as before.
- **Follow-up instruction** - operation `subagent.instruct {session_id, prompt}` (prompt 1-32768 characters, own
  record, audit and `operation.*` rows like every operation; duplicate protection through the idempotency key, the
  guest's `prompt_id` = the operation ID, so a repeated delivery never types twice). The runner checks locally that
  `session_id` is a child of the requesting session in the same task that has not ended (`rejected` `not_found`
  otherwise; the hub cannot read the parameters). The executor resumes a suspended child, hands the prompt to the
  child's guest with the child's sender line (ADR-083 §1: written by the parent session, which works on an
  instruction of the acting user of this operation) and succeeds with `{session_id, instruction_id}` once the guest
  registered it. Instructions of one child are handed over **one at a time in the order of their creation, one per
  turn**: the next waits until the child accepted the previous one and that turn ended, so every instruction has its own
  turn and result. Resuming a suspended child makes it running again, so the runner pre-checks
  `subagent_max_running` for it (`limit_exceeded`; the hub cannot read the target).
- **End** - operation `subagent.stop {session_id}` (same local check) stops the child (`stopped`); the child also
  ends when its parent ends (`stopped`/`failed`: the runner stops its children), when the task is closed (as every
  session), and when the runtime setting (§2) is reached (`failed` `runtime_exceeded`).
- **Result** - `get_subagent_result {session_id, wait_seconds?}` answers the newest instruction: `working` with its
  `instruction_id` while that turn runs; `idle`/`suspended` with `final_text`, `finished_at` and `instruction_id` once
  it ended; `stopped` with the last turn's `final_text`; `failed`. `wait_seconds` waits for the end of the newest
  instruction's turn (or of the child). The text is read from the outbox as before (ADR-038), so a turn read an hour or
  more after it ended answers an empty `final_text`.
- **Rights** are the parent's (A1): the instructing human of a child's turn is the acting user of the instruction that
  turn works on - set when the child accepts the instruction's prompt (prompt ID = operation ID), like the turn actor of
  a main session - and it decides the child's reads (ADR-089), its operations' `triggered_by` and its personal
  connections (ADR-070) through the one function `credentials.acting.stamp`.
- **Older guests.** A guest before this ADR (golden with guest version < 0.3.0) validates `get_subagent_result`
  against the former contract (no result while `idle`) and does not know the new operation types. Children whose VM
  runs such a guest keep the one-shot end of ADR-038 (stopped after their first turn) until the VM runs the new golden;
  the runner reads the guest version from the VM's golden record. The ping `subagent.finished` now means "a turn of the
  child ended"; the harness says so, and its sentences about follow-ups and cross-workgroup reads reach only guests
  that know them.

### 2. Every limit is one setting with a generous default
| Setting (hub environment) | Default | Meaning (0 = no limit) |
|---|---|---|
| `AGENT_HUB_SUBAGENT_MAX_DEPTH` | 5 | nesting depth of children (main session 0) |
| `AGENT_HUB_SUBAGENT_MAX_RUNNING` | 50 | children of one task that have not ended and are not suspended |
| `AGENT_HUB_SUBAGENT_MAX_TOTAL` | 0 | children of one task in total |
| `AGENT_HUB_SUBAGENT_MAX_RUNTIME_SECONDS` | 0 | time since a child's creation after which it is failed `runtime_exceeded` |

- **One source.** The hub reads them once, enforces depth, running and total in check H4, publishes all four in
  `GET /v1/config` `limits` and in the device state section `limits` (`GET /v1/device/state`). The runner takes every
  value from the device state: the pre-check, the runtime end, `get_context` and the harness line. No component keeps
  a copy; a runner talking to an older hub (no section) does no local pre-check (the hub decides) and shows the
  limits as unknown.
- **Room of the VM.** Children run in their parent's VM. Before a child is started or resumed the runner also checks the
  VM's room (`AGENT_RUNNER_SESSIONS_PER_VM`, ADR-075 §1): a full VM refuses with `limit_exceeded` and the sentence
  naming that setting, never with a crash of the VM. *Replaced (2026-11-27, HOT-32, ADR-075 addendum 5): a child is
  never refused for the room of its VM - it belongs to running work; the reserve for children is kept when a new task
  is placed (05-runner.md §3.3.1).*
- **Clear message.** A request over a limit ends `rejected` (runner pre-check) or `denied` (hub) with `limit_exceeded`
  and a sentence that names the limit, its value and the setting, e.g. "This task already has 50 running subagents,
  the limit of this platform (setting subagent_max_running). Stop one with subagent.stop or wait until one ends." The
  situation view shows the task as `blocked` with the new reason `limit_reached` (KOR-39 table, ADR-076).
- **Visible through the interfaces (E-04):** `GET /v1/config` (everyone), `get_context.limits` and the harness context
  (the session). Changing a value is an operator setting like the other hub limits (ADR-075 §1), so the browser gains
  no new action.

> **Addendum (2026-11-26): no number of running children.** ADR-075 addendum 2 removes every fixed number of what
> may run at once, `AGENT_HUB_SUBAGENT_MAX_RUNNING` included: an older hub environment that sets it starts, the value
> is ignored and named once. How many children run is decided by the measured room of their VM (`vm.room`, ADR-075
> addendum 1; the former `AGENT_RUNNER_SESSIONS_PER_VM` is gone as well): a VM without free resources refuses a child or
> the resume of a suspended one with `limit_exceeded` and the sentence "The computer of this task has no free
> resources for one more session right now (its RAM or CPU is used up). …". Depth, total and runtime stay settings.
> For older peers the hub keeps publishing `subagent_max_running`, always 0 (no limit); the runner reads it past and
> its harness line says "no limit of running subagents".

### 3. Reporting never cuts
- The guest's `GET /v1/health` and `GET /v1/metrics` list **every** session of the VM (the 64 cap and the runner's
  refusal of more are gone; the contracts drop the `maxItems`).
- The runner's heartbeat sends every open session and every VM: when they do not fit into one frame of 64 KiB, it sends
  several heartbeat frames in a row, each a complete heartbeat with its share of the sessions and VMs (the hub uses the
  frames independently: capacity is the same in each).
- The metrics outbox already splits samples into items of at most 1000.

### 4. The class of error is closed (E-19)
`tests/platform/test_fixed_limits.py` reads, in `agent_hub`, `agent_runner` and `agent_guest`, every constant and
dataclass field whose name contains `MAX` or `LIMIT` (in any position), every slice with a literal bound (`[:64]`) and
every SQL `LIMIT` with a literal number, and in `contracts/` every `maxItems`/`maximum`, and refuses any that is not
covered by the reasoned inventory `tests/platform/fixed_limits.json`: sizes of one message or field,
page sizes of paged reads (KOR-35/ADR-086), protocol and integer bounds, time windows, rate limits, and the defaults
and bounds of settings. A count of things a person or session creates is no allowed reason; a new one fails the test
until it is a setting.

### 5. Rollout order
Hub first (it accepts the new operation types and publishes the limits), then the runner (it uses them, clamps
`get_context` for older guests and keeps their children one-shot), then the golden (the guest that lists every session
and knows the new results). Every other order keeps working except that an older runner refuses a guest metrics answer
of more than 64 sessions and an older hub refuses the new operation types (`unknown_type`).

## Consequences
- A main agent can hold a conversation with its subagents and decides when they end; idle children cost no CPU once
  suspended.
- Operators change limits without a release; the defaults no longer stop normal work.
- New operation types `subagent.instruct`, `subagent.stop` (contracts, hub type list, runner checks and executors, web
  texts, harness examples).

## Alternatives considered
- Keep one-shot children and let the parent start a new child per question: loses the child's context (rejected,
  Idee 26 "vollwertig", A10).
- Limits as an organization setting edited by admins: the hub enforces them, so they would be plaintext metadata of
  the organization with a new route, screen and audit; not needed for A5 now and possible later beside the operator
  default.
- Truncating monitoring with a "truncated" flag: still blind for the cut sessions (A14).

## Deviation from DESIGN.md
None beyond ADR-001.
