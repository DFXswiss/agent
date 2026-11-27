-- CI VMs, stage 1 (docs/platform/spec/16-ci-runners.md section 17; decisions D-12 and D-13).
--
-- A CI VM is a `runner.workvm` row of scope `ci`: one fresh VM per GitHub Actions job. `task_id`, `org_id` and `wg_id`
-- name the task the job belongs to (the task of the pull request a session of the platform created, or the fixed task
-- of the repository), `session_id` and `requested_by` the session that bears its runtime (the session that created the
-- pull request, or the fixed task's main session). New teardown reasons: `ci_ended` (the runner process in the VM
-- exited), `ci_timeout` (the wall-clock limit of the job passed) and `ci_cancelled` (the controller ended it: a runner
-- that never got its job, a job that was not taken, the controller stopped).
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_scope_check;
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_delete_reason_check;
ALTER TABLE runner.workvm
    ADD CONSTRAINT workvm_scope_check CHECK (scope IN ('task', 'session', 'run', 'browser', 'ci')),
    ADD CONSTRAINT workvm_delete_reason_check CHECK (delete_reason IN ('released', 'idle', 'session_ended',
        'task_closed', 'failed', 'restart', 'run_ended', 'credit_exhausted', 'session_paused', 'browser_closed',
        'browser_idle', 'vm_lost', 'ci_ended', 'ci_timeout', 'ci_cancelled'));
