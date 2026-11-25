-- The platform browser (KOR-44, ADR-085; docs/platform/spec/13-work-vm.md section 13).
--
-- A browser VM is a `runner.workvm` row of scope `browser`: a VM of the requesting session (`session_id`) that its open
-- browser session drives. New teardown reasons: `browser_closed` (`browser.close`), `browser_idle` (no browser call
-- for the session's `idle_seconds`) and `vm_lost` (the VM stopped answering; a new one follows on the next call).
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_scope_check;
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_check;
ALTER TABLE runner.workvm DROP CONSTRAINT workvm_delete_reason_check;
ALTER TABLE runner.workvm
    ADD CONSTRAINT workvm_scope_check CHECK (scope IN ('task', 'session', 'run', 'browser')),
    ADD CONSTRAINT workvm_check CHECK ((scope = 'task') = (session_id IS NULL)),
    ADD CONSTRAINT workvm_delete_reason_check CHECK (delete_reason IN ('released', 'idle', 'session_ended',
        'task_closed', 'failed', 'restart', 'run_ended', 'credit_exhausted', 'session_paused', 'browser_closed',
        'browser_idle', 'vm_lost'));

-- `browser_session`: one row per browser session (`browser.open`, or a browser tool of a session without one:
-- `implicit`, no operation). `seq` counts the actions (`browser.action`), `frame_seq` the live frames (a counter of
-- its own). `workvm_id` is the VM it drives now (null before the first VM and after it went). A row stays `open`
-- across a lost VM and a runner restart (the next call obtains a fresh VM); it is `closed` with the reason of its end.
CREATE TABLE runner.browser_session (
    browser_id text PRIMARY KEY CHECK (browser_id ~ '^br-[0-9a-f]{12}$'),
    operation_id uuid,
    session_id uuid NOT NULL,
    task_id uuid NOT NULL,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    workvm_id text,
    state text NOT NULL CHECK (state IN ('open', 'closed')),
    seq bigint NOT NULL DEFAULT 0 CHECK (seq >= 0),
    frame_seq bigint NOT NULL DEFAULT 0 CHECK (frame_seq >= 0),
    idle_seconds integer NOT NULL CHECK (idle_seconds BETWEEN 60 AND 3600),
    url text NOT NULL DEFAULT '',
    ready_ms integer NOT NULL DEFAULT 0 CHECK (ready_ms >= 0),
    implicit boolean NOT NULL DEFAULT false,
    opened_at timestamptz NOT NULL,
    last_call_at timestamptz NOT NULL,
    closed_at timestamptz,
    close_reason text CHECK (close_reason IN ('browser_closed', 'browser_idle', 'session_ended', 'task_closed',
        'credit_exhausted')),
    CHECK ((state = 'closed') = (closed_at IS NOT NULL AND close_reason IS NOT NULL)),
    CHECK (implicit = (operation_id IS NULL))
);
CREATE UNIQUE INDEX browser_session_open ON runner.browser_session (session_id) WHERE state = 'open';
