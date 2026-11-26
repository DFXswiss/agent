-- Login VMs (KOR-73, ADR-098 section 3): the browser sandbox of one Claude login is a `runner.workvm` row of scope
-- `login` (`session_id` = the login request, task, organization and workgroup the nil UUID). New teardown reason:
-- `login_ended` (the login is over, on every path).
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_scope_check;
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_delete_reason_check;
ALTER TABLE runner.workvm
    ADD CONSTRAINT workvm_scope_check CHECK (scope IN ('task', 'session', 'run', 'browser', 'login')),
    ADD CONSTRAINT workvm_delete_reason_check CHECK (delete_reason IN ('released', 'idle', 'session_ended',
        'task_closed', 'failed', 'restart', 'run_ended', 'credit_exhausted', 'session_paused', 'browser_closed',
        'browser_idle', 'vm_lost', 'login_ended'));
