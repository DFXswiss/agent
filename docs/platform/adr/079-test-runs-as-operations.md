# ADR-079: Test runs as platform operations – a fresh VM per run, kept logs, billed and well-ended work VMs

Status: Accepted (2026-11-24)

## Context
- Concept idea 27 ([KONZEPT](../KONZEPT.md#k-idee-27)): lint, formatting, unit and integration tests are requested
  through the platform; every run gets an isolated temporary Lima VM; results, logs and exit codes are kept; the VM
  goes afterwards, also on failure. §10 point 3 ([KONZEPT](../KONZEPT.md#k-10-b3)) and §11 point 1
  ([KONZEPT](../KONZEPT.md#k-11-b1)): every operation a session requests is a record with ID, task and session, type,
  parameters, times, state and result.
- Idea 46 ([KONZEPT](../KONZEPT.md#k-idee-46)): DFX VM runtime is billed to the second, test VMs included. Idea 47
  ([KONZEPT](../KONZEPT.md#k-idee-47)): without credit no further paid resources; running paid work stops in a
  controlled way. Idea 39 ([KONZEPT](../KONZEPT.md#k-idee-39)): a closed task frees its resources.
- Rules and interpretations: [E-08](../KONZEPT.md#e-08) and [E-09](../KONZEPT.md#e-09) (the work VM stays free and
  unwatched), [E-11](../KONZEPT.md#e-11) and [E-25](../KONZEPT.md#e-25) (no restriction or protection layer beyond
  what was ordered), [E-04](../KONZEPT.md#e-04) (agents reach everything), [A6](../KONZEPT.md#a-6),
  [A8](../KONZEPT.md#a-8), [A9](../KONZEPT.md#a-9) (a test run is its own operation with record, result, exit code
  and encrypted log; every run gets its own throw-away VM, even when slower; the start time is measured).
- Built before: the work VM of ADR-059 (one VM per task or session, tools `workvm_*`). Its `workvm.run` event kept only
  the last 64 KiB of a command's output; work VMs were not billed (the meter's extension point `billing_vms.py` had no
  provider); the reason `task_closed` existed in the schema but was never set; a paused session kept its VM until the
  idle teardown; the implicit start through `workvm_exec` showed no countdown.

## Decision

### 1. Operation `test.run`
- Params `{kind: "lint"|"format"|"unit"|"integration"|"security"|"custom", command, cwd?, timeout_seconds?, size?}`:
  `command` 1–65536 characters (run as `bash -lc` as root, like `workvm_exec`), `cwd` absolute
  (default `/workspace`), `timeout_seconds` 1–86400 (default 3600), `size` `standard|large|xlarge` (default
  `standard`, capped by `WORKVM_MAX_SIZE`). No fixed templates: the session writes the command it needs (E-11); `kind`
  labels the run for people and lists.
- Result `{run_id, state: "passed"|"failed"|"error"|"timed_out"|"cancelled", exit_code, duration_ms, ready_ms,
  summary, log, tail, reason}`: `passed` = exit code 0, `failed` = another exit code, `timed_out` = the time limit
  ended it, `cancelled` = it was ended (credit used up), `error` = the platform could not run it (start, transfer,
  restart). `summary` `{passed, failed, skipped, errors}` (each an integer or null) when the output has a line the
  runner recognises (pytest, vitest/jest, go test, cargo test, mocha, ESLint/ruff problem counts), else null. `log`
  `{object_id, bytes, stored_bytes, truncated}` names the complete output as an encrypted object (§3), `tail` the
  last ≤ 8192 characters, `reason` a closed code for `error`/`cancelled`.
- The operation succeeds whenever the command started (also when tests are red); it fails only when nothing ran
  (`workvm_unavailable`, `credit_exhausted`, `limit_exceeded`, `timeout` of the capacity queue, `upstream_error` when
  the workspace cannot be read or the VM does not start – retryable). A failed transfer or a VM that stops answering
  after the VM started ends the run `error` (`transfer_failed`, `start_failed`, `vm_lost`).
- Dedupe (§11): the hub does not dedupe (`dedupe: false`, like `workvm.request`), because only the runner knows the
  state of the workspace. The runner joins a request to a run of the **same session** that is still running with the
  same `kind`, `command`, `cwd`, `size`, `timeout_seconds` and the same workspace digest (SHA-256 over the export
  manifest's paths and hashes) while it is queued, starting or running: both operations get the same `run_id` and
  result. A finished run is never joined; asking again runs again.
- Harness: tests, lint and formatting go through `test.run`; everything else keeps running in the work VM, which stays
  unrestricted (E-08). `workvm_exec` refuses nothing.

### 2. A fresh VM per run
- A run VM is a row of `runner.workvm` with scope `run` (migration): it takes a slot, RAM, disk and the warm pool like
  a work VM, waits in the same FIFO queue, has the same outside boundary (no credential, host filter, egress through
  the device's Cloudflare client, fail-closed; including the known limit of the per-account filter, 13-work-vm.md
  §6.6, which the operator accepted on 2026-11-24), is sampled by monitoring as a work VM of its session (device → VM
  → session) and is billed (§4). Run VMs count against the organization and owner VM limits, not against the per-task
  limit (a task's work VMs would otherwise block its tests; the device's capacity bounds them).
- **Workspace transfer: mirror, not image.** The session workspace (the guest export, as `workvm_sync to_vm` sends
  it) is pushed into `/workspace` of a fresh pool clone. Cloning the work VM would need it stopped (it runs the
  session's services, E-08) or a copy of its whole disk (tens of GiB); the mirror is the state the session commits
  and reproducible. Files changed only inside the work VM come along after `workvm_sync from_vm` (the harness says
  so). Dependencies are installed by the command itself (`npm ci && npm test`) – the image carries the toolchains. A
  dependency cache across runs is a later optimisation (E-25); the start time is measured (`ready_ms`, log line
  `runner.testrun ready`).
- **Time limit on the runner's clock**: the runner polls the job every 5 s and kills it (`KILL`) when
  `timeout_seconds` passed (`timed_out`); `RuntimeMaxSec` in the VM is only a second bound (a VM root could change it).
- **Always torn down**: the executor reads the tail, stores the log (at most 300 s) and deletes the VM in a `finally`
  shielded from cancellation (end, error, time limit, cancel). A failed Lima deletion keeps the row `deleting` (the
  slot is not given back) and the daemon task's sweep deletes it again every minute, together with own `wvm-`
  instances without a row. After a restart the work VM reconciliation deletes every live VM (reason `restart`); the
  run reconciliation marks every run still `running` as `error` (reason `restart`) and writes its final event. The
  pipeline fails the operation found `executing` (`timeout`, retryable) as before: a run is never executed twice.
- **Cancellation**: a session that ends (`session_ended`) or a task that closes (`task_closed`) ends its runs
  `cancelled` within one poll; credit used up ends them `cancelled` (`credit_exhausted`). Stopping a single run by a
  person is not built (the person stops the session).
- Teardown reasons (migration): `run_ended`, `credit_exhausted`, `session_paused`.

### 3. Logs are kept encrypted (reuse of the context objects, ADR-072)
- The complete output of a run is read from the VM before the teardown (raw bytes, pieces of 1 MiB) and stored as an
  object of kind `log` in the context object store: chunks of at most 256 KiB **uncompressed** output under labels of
  their own (`k_log` with info `run-log\0`, AAD `run-log-chunk\0`, signature label `run-log\0`), and a manifest that
  names `kind: "log"`, `ref` (the run or job ID), `bytes` and `stored` (04-crypto.md §4.12). A log can therefore never
  pass for a context checkpoint or the reverse (review finding: the shared labels would have let a hub offer run output
  as a checkpoint), and a reader checks `ref` against the encrypted event that named the log (no swap of two logs of a
  session). Uncompressed fixed-size chunks let a reader fetch any window.
- Quota as setting: `AGENT_RUNNER_LOG_MAX_MIB` (default 256) per log; a longer output keeps its last part
  (`truncated`). At the hub, logs have their own organization quota `AGENT_HUB_LOG_ORG_BYTES` (default 100 GiB, so they
  never crowd out the checkpoints a resume needs) and `AGENT_HUB_LOG_KEEP` (default 500) complete logs per task;
  context listing, pruning, the device state's `context`, the device downloads and the runner's restore candidates see
  only kind `context`.
- Upload: the runner's existing upload loop (`contexts/checkpoint.py`), with `kind: "log"` in the chunk body; a log may
  belong to a subagent session. A log the hub expired is uploaded again under the same ID (its manifest stays valid);
  a log leaves the runner's cache once the hub holds it or refused it for good and is never evicted before.
- Reading: `GET /v1/tasks/{task}/logs/{object}` (manifest, signature, times) and `.../chunks/{index}` (`{ct}`) for
  every reader of the task (`task:read`, read log `read.task.log` / `read.task.log.chunk`); the browser and
  `agent-platform test log` open the chunks with the workgroup key. The hub never sees plaintext.
- **Work VM commands too**: when a `workvm_exec`/`workvm_browse` job ends with more output than its event keeps
  (64 KiB), the runner stores the full output as a `log` object in the background and the final `workvm.run` event
  names it (`log`); the teardown of its VM waits for pending captures (at most 300 s).

### 4. Billing, credit and paused sessions
- `workvm/billing_vms.py:VMS` gives every `running` work VM and run VM to the meter of ADR-062 (`vm_kind` `work`, the
  VM's size), on platform devices only (the meter's rule). A task VM is split evenly between the task's sessions in a
  holding state (never `suspended`), like a workgroup VM; when none holds it its requester bears it. A session VM and a
  run VM belong to their session. The meter's tick (5 s) bounds the precision at both ends of a VM, as for session VMs
  (an open point of the meter, not changed here).
- Credit used up (an exhausted organization on a platform device): `workvm.request`, an implicit request, a restart of
  a stopped VM and `test.run` fail `credit_exhausted` with a clear message; a running run is killed and ends
  `cancelled` (`credit_exhausted`); the reconcile step `workvm` stops the organization's running work VMs at once.
- **One rule for paused sessions**: when the guest reports a session `suspended`, the VMs it works in are stopped
  (`limactl stop`, state `stopped`: no RAM, no runtime billed) – the task VM only when no other session of the task
  is active – unless a job still runs in it (review: a long build a session waits for must not be killed by its idle
  suspension; the idle pass decides as before). The credit stop is immediate (above). A stopped VM is deleted after
  `WORKVM_IDLE_SECONDS`; a call or request of the resumed session starts it again (its disk keeps what was
  installed), or deletes it and gets a new one when its RAM does not fit. A waiting request gets the slot of the
  oldest stopped VM. Run VMs are never stopped.

### 5. End of life and visibility
- Closing a task: the hub marks the task's sessions in the device state with `task_closed: true`; the reconcile step
  `workvm` deletes every VM of the task (task, session and run VMs) at once with reason `task_closed` and cancels its
  runs. When the hub's stop hint ends a session of the task first, its VMs go at once with the session (reason
  `task_closed` when the runner already knows the task is closed, else `session_ended`).
- Implicit start: `workvm_exec`/`workvm_browse` without a VM write `workvm.state` `starting` first, so the panel shows
  the countdown as for `workvm.request`.
- Event `test.run` (scope `wg`, author the device): written when a run starts (`running`) and when it ends, with the
  result fields plus `operation_id`, `kind`, `command` (≤ 4096 characters), `cwd`, `size`, `started_at`, `ended_at`.
  Web: chat card "Tests: 3 failed – open log", the operation card, the session's runs in the work VM panel, a windowed
  log viewer with download. `agent-platform test list|show|log`.

## Consequences
- The hub learns no content: run results and logs are encrypted; the hub stores the operation record, sealed chunks
  and the sizes it needs for quotas.
- A run costs the VM start plus the dependency installation of the command; measured, not hidden (A9).
- The hub sees sizes and counts of logs and runs (metadata it needs for quotas), never content; operation records
  carry only the encrypted result.
- Out of scope: stopping a single run by a person, a dependency cache across runs, browser VMs with live picture (KOR-44), restart of temporary VMs after
  a device failure (KOR-58), roles that use `test.run` (KOR-51).
