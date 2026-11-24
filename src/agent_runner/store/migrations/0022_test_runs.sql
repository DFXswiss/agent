-- Test runs as platform operations (KOR-43, ADR-080; docs/platform/spec/13-work-vm.md section 12).
--
-- A run VM is a `runner.workvm` row of scope `run`: a fresh VM of the requesting session (`session_id`) for one
-- `test.run`, deleted when the run ends (`run_ended`). New teardown reasons: `run_ended`, `credit_exhausted` (the
-- organization's credit is used up) and `session_paused` (a paused session's VM was stopped and then not used again).
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_scope_check;
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_check;
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_delete_reason_check;
ALTER TABLE runner.workvm
    ADD CONSTRAINT workvm_scope_check CHECK (scope IN ('task', 'session', 'run')),
    ADD CONSTRAINT workvm_check CHECK ((scope = 'task') = (session_id IS NULL)),
    ADD CONSTRAINT workvm_delete_reason_check CHECK (delete_reason IN ('released', 'idle', 'session_ended',
        'task_closed', 'failed', 'restart', 'run_ended', 'credit_exhausted', 'session_paused'));

-- `test_run`: one row per run (the operation that started it; a joined duplicate has no row of its own). `digest` is
-- the SHA-256 of the workspace export manifest the run started from (the duplicate rule). `state` `running` until the
-- run ended; `result` is the operation result (`contracts/ops/test.run.result.json`) once it ended. A row still
-- `running` after a restart ends `error` with the reason `restart` (its VM went with the work VM reconciliation).
CREATE TABLE runner.test_run (
    run_id text PRIMARY KEY CHECK (run_id ~ '^tr-[0-9a-f]{12}$'),
    operation_id uuid NOT NULL UNIQUE,
    session_id uuid NOT NULL,
    task_id uuid NOT NULL,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    workvm_id text,
    kind text NOT NULL,
    command text NOT NULL,
    cwd text NOT NULL,
    size text NOT NULL,
    timeout_seconds integer NOT NULL CHECK (timeout_seconds > 0),
    digest text,
    state text NOT NULL CHECK (state IN ('running', 'passed', 'failed', 'error', 'timed_out', 'cancelled')),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    result jsonb
);
CREATE INDEX test_run_running ON runner.test_run (session_id) WHERE state = 'running';

-- Logs (ADR-080 section 3): the complete output of a test run or of a long work VM job, sealed and uploaded like a
-- context checkpoint. Restores and the newest-object rule of the cache see only `context`; a log leaves the cache once
-- the hub holds it.
ALTER TABLE runner.context_object
    ADD COLUMN kind text NOT NULL DEFAULT 'context' CHECK (kind IN ('context', 'log'));
