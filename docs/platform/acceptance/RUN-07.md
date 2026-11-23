# RUN-07 acceptance evidence (operation pipeline)

## Unit and integration tests (`make platform-ci`)
- `tests/platform/runner/operations/`: `test_rules.py`, `test_checks.py`, `test_dedupe.py`, `test_register.py`,
  `test_pipeline.py`, `test_duplicates.py`, `test_inbound.py`, `test_wiring.py` (kit `kit.py`).
- Real PostgreSQL store, the real sync runtime (outbox, sealer, signed hub client) and the verified logs and keys of
  the RUN-03 rig. The hub double answers `POST /v1/device/operations` from a script and validates every request and
  answer against `api/device-operations.{request,response}.json`; params events are checked with the device key and
  opened with the workgroup key; pushed outbox items pass the contract checks of the sync hub double. Executors and
  pre-checks are fakes; the clock is the rig's fake clock.
- Covered:
  - every rule of every type of 03 §9, positive and negative, with exact codes (`unknown_type`, `params_invalid` with
    pointers, `path_invalid`, `repo_not_allowed`, `branch_protected`, `github_not_connected`, `not_found`,
    `model_not_allowed`), the check order, code-point lengths, effective parameters; the rule constants and
    examples of `contracts/ops/rules.json` (repo, branch, ref, path, protected patterns, title, secret patterns);
  - dedupe tags against `contracts/vectors/dedupe.json` (every type with dedupe fields, idempotency key, invalid
    vectors), epoch dependency;
  - pipeline states: `received` → `accepted` → `executing` → `succeeded`/`failed`; `rejected` (runner and guest
    rejections, binding), `denied`, `duplicate`, `expired`; the stored registration re-sent after a restart;
    `executing` after a restart → `failed timeout` without a second execution;
  - order: outbox `operation_transition executing`, (executor events), `event operation.result`, then
    `operation_transition succeeded|failed` with `result_event_id`; then the pending answer;
  - retries with the fake clock: 2 s, 8 s, 30 s then `failed retryable:true`; `retry_after`; `retry_after` > 60 s;
    non-retryable and crashing executors;
  - registration retries (transport errors, 503, 429, decisions outside the contract) with identical bodies; expiry
    after 10 min (`hub_unreachable`); final refusals (403, 400); waiting for the workgroup key;
  - duplicate paths: local original (one execution, one POST), another device's pulled `operation.result`, failed
    original, nothing final within 20 s → `pending` with `duplicate_of`, late duplicates, `get_operation` answers;
  - `existing:true` → MCP `duplicate`; size limits of params and result events; `task.set_title` → device-authored
    `task.meta`;
  - inbound: journal handler (once per operation ID, NOTIFY after commit, foreign sessions, malformed bodies),
    settings events (latest wins, invalid refused and audited, tampered → defaults), result events (valid, invalid
    refused and audited);
  - wiring: registrations, extension points (`op_prechecks`, `op_finished`), loop, listener, daemon task,
    `pipeline_for`.
- Coverage of `agent_runner/operations`: 100 % lines and branches.

## E2E / ATK
E2E-21, E2E-22, E2E-24 (SCN-04) and ATK-33 (ATS-02) do not exist yet; they are written by their packages.
