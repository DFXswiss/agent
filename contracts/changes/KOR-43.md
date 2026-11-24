# KOR-43 – contract changes

ADR-027 section 3. Additive only (ADR-080).

- `ops/test.run.params.json`, `ops/test.run.result.json`, `ops/INDEX.json` (`dedupe: false`): the operation `test.run`
  (`ops/objects.json` `params_test_run`, `result_test_run`, `test_run_id`, `test_run_kind`, `test_run_state`,
  `test_run_reason`, `test_run_summary`, `log_ref`).
- `events/test.run.json`, `events/kinds.json`, `enums/event-kinds.json`, the header kind enum and the
  `operation.params` variant in `events/objects.json`: the event kind `test.run` (scope `wg`, author the device).
- `events/objects.json` `body_workvm_run`: optional `log` (`log_ref`, the full output of a job longer than its tail).
- `events/objects.json` `body_workvm_state`: states `starting` (implicit request) and `stopped` (paused session),
  reasons `session_paused` and `credit_exhausted`.
- `api/device-context-chunk-put.request.json`: optional `kind` (`context`, `log`), read with the first chunk.
- `api/task-log*.json`, `api/INDEX.json`: `GET /v1/tasks/{task}/logs/{object}` and `.../chunks/{index}` (browser,
  `task:read`, read log `read.task.log`, `read.task.log.chunk`; `enums/audit-actions.json`).
- `api/device-state.response.json`: optional `task_closed` per session.
- `audit/read.task.log.json`, `audit/read.task.log.chunk.json` (read log); `audit/session.context_saved.json` also for logs (`target_kind` `log`).
- `common.json` `log_manifest` (04-crypto.md section 4.12); `api/device-context-commit.request.json` takes either
  manifest; `api/task-log.response.json` names the log manifest.
