# ADR-078: Test runs as platform operations – a fresh VM per run, kept logs, billed and well-ended work VMs

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
- The operation succeeds whenever the run happened (also when tests are red); it fails only when the platform cannot
  run it (`workvm_unavailable`, `credit_exhausted`, `limit_exceeded`, `timeout` of the capacity queue).
- Dedupe (§11): the hub does not dedupe (`dedupe: false`, like `workvm.request`), because only the runner knows the
  state of the workspace. The runner joins a request to a run of the **same session** that is still running with the
  same `kind`, `command`, `cwd`, `size`, `timeout_seconds` and the same workspace digest (SHA-256 of the export
  manifest): both operations get the same `run_id` and result. A finished run is never joined; asking again runs
  again.
- Harness: tests, lint and formatting go through `test.run`; everything else keeps running in the work VM, which stays
  unrestricted (E-08). `workvm_exec` refuses nothing.

### 2. A fresh VM per run
- A run VM is a row of `runner.workvm` with scope `run` (migration): it takes a slot, RAM, disk and the warm pool like
  a work VM, waits in the same FIFO queue, has the same outside boundary (no credential, host filter, egress through
  the device's Cloudflare client, fail-closed), is sampled by monitoring as a work VM of its session (device → VM →
  session) and is billed (§4). Run VMs do not count against the per-task, per-organization and per-owner VM limits
  (they are short and bounded by the device's capacity; E-11).
- **Workspace transfer: mirror, not image.** The session workspace (the guest export, as `workvm_sync to_vm` sends
  it) is pushed into `/workspace` of a fresh pool clone. Cloning the work VM would need it stopped (it runs the
  session's services, E-08) or a copy of its whole disk (tens of GiB); the mirror is the state the session commits,
  so a run is reproducible and equals what CI sees. Dependencies are installed by the command itself (`npm ci && npm
  test`, `pip install -e . && pytest`) – the image carries the toolchains. A dependency cache across runs is a later
  optimisation; the start time is measured and reported (`ready_ms`, log line `runner.testrun ready_ms= pooled=`).
- **Always torn down**: the executor deletes the VM in a `finally` (end, error, time limit, cancel). After a restart
  the work VM reconciliation deletes every live VM (reason `restart`) and every own `wvm-` instance; the run
  reconciliation marks every run still `running` as `error` (reason `restart`) and writes its final event. The
  pipeline fails the operation found `executing` (`timeout`, retryable) as before: a run is never executed twice.
- Teardown reasons (migration): `run_ended`, `credit_exhausted`, `session_paused`.

### 3. Logs are kept encrypted (reuse of the context objects, ADR-072)
- The complete output of a run is read from the VM before the teardown (raw bytes, pieces of 1 MiB) and stored as an
  object of kind `log` in the context object store: chunks of at most 256 KiB **uncompressed** output, sealed exactly
  like a context chunk (`k_ctx` of the workgroup epoch key, AES-256-GCM, AAD with object ID and index), manifest
  signed by the device. Uncompressed fixed-size chunks let a reader fetch any window (`offset // 256 KiB`).
- Quota as setting: `AGENT_RUNNER_LOG_MAX_BYTES` (default 256 MiB) per log; a longer output keeps its last part
  (`truncated: true`, `bytes` = the full length). The hub's per-object and per-organization quotas of ADR-072 apply;
  `AGENT_HUB_LOG_KEEP` (default 500) complete logs per task are kept, older ones are pruned at the commit. Context
  pruning, listing and restore see only kind `context`.
- Upload: the runner's existing upload loop (`contexts/checkpoint.py`), with `kind: "log"` in the chunk body of the
  first chunk; a log is never "due" again after an expiry (it is refused for good, the local copy goes with the cache
  eviction). A log may belong to a subagent session.
- Reading: `GET /v1/logs/{object}` (manifest, signature, times) and `GET /v1/logs/{object}/chunks/{index}` (`{ct}`)
  for every reader of the workgroup (`wg:read`, read log `read.log`); the browser and `agent-platform` open the chunks
  with the workgroup key. The hub never sees plaintext (Zero-Knowledge).
- **Work VM commands too**: when a `workvm_exec`/`workvm_browse` job ends with more output than its event keeps
  (64 KiB), the runner stores the full output as a `log` object in the background and the final `workvm.run` event
  names it (`log`); the teardown waits for pending captures of its VM.

### 4. Billing, credit and paused sessions
- `workvm/billing_vms.py:VMS` gives every `running` work VM and run VM to the meter of ADR-062 (`vm_kind` `work`, the
  VM's size, the requesting session bears the whole interval), on platform devices only (the meter's rule).
- Credit used up (an exhausted organization on a platform device): `workvm.request`, an implicit request and
  `test.run` fail `credit_exhausted` with a clear message; a running run is killed and ends `cancelled` with reason
  `credit_exhausted`.
- **One rule for paused sessions** (suspended by idle, by the user or for credit): the session's VM is stopped at once
  (`limactl stop`, state `stopped`: no RAM, no runtime billed) – the task VM only when no other session of the task is
  active – and deleted after `WORKVM_IDLE_SECONDS` stopped. A call or request of the resumed session starts it again
  (its disk with installed tools is still there); when its RAM does not fit any more, it is deleted and a new VM is
  requested. Its slot is given back first to a waiting request (oldest stopped VM).

### 5. End of life and visibility
- Closing a task: the hub marks the task's sessions in the device state with `task_closed: true`; the reconcile step
  `workvm` deletes every VM of the task at once with reason `task_closed`.
- Implicit start: `workvm_exec`/`workvm_browse` without a VM write `workvm.state` `starting` first, so the panel shows
  the countdown as for `workvm.request`.
- Event `test.run` (scope `wg`, author the device): written when a run starts (`running`) and when it ends, with the
  result fields plus `operation_id`, `kind`, `command` (≤ 4096 characters), `cwd`, `size`, `started_at`, `ended_at`.
  Web: chat card "Tests: 3 failed – open log", the session's runs in the work VM panel, a windowed log viewer with
  download. `agent-platform test list|show|log`.

## Consequences
- The hub learns no content: run results and logs are encrypted; the hub stores the operation record, sealed chunks
  and the sizes it needs for quotas.
- A run costs the VM start plus the dependency installation of the command; measured, not hidden (A9).
- Out of scope: a dependency cache across runs, browser VMs with live picture (KOR-44), restart of temporary VMs after
  a device failure (KOR-58), roles that use `test.run` (KOR-51).
