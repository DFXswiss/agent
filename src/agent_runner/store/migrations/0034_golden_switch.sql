-- Workgroup VMs move to the current golden by themselves, paused sessions move with them (KOR-70, ADR-093).
--
-- `context_session.checkpoint_state`: what the newest checkpoint request of the session ended with - the workspace
-- state of the archive (`full`, `partial`, `omitted`, `empty`; `archive.Context.workspace_state`) when it was taken or
-- identical, `dropped` when it ended without a checkpoint. NULL for sessions checkpointed before this migration (their
-- completeness is unknown: the switch takes a fresh checkpoint first).
-- `golden_switch`: one row per workgroup VM that was replaced by a VM of a newer golden while sessions on it were
-- paused (`vm.golden_switch`); the restore of a moved session names the new version in its note.
ALTER TABLE runner.context_session ADD COLUMN checkpoint_state text
    CHECK (checkpoint_state IN ('full', 'partial', 'omitted', 'empty', 'dropped'));

CREATE TABLE runner.golden_switch (
    old_vm_id text PRIMARY KEY CHECK (old_vm_id ~ '^vm-[0-9a-f]{8}$'),
    new_vm_id text NOT NULL CHECK (new_vm_id ~ '^vm-[0-9a-f]{8}$'),
    golden_version text NOT NULL,
    switched_at timestamptz NOT NULL
);

-- `golden_switch_hold`: since when a cause holds an outdated workgroup VM on its golden (`vm.golden_switch`, reported
-- to the hub as `upgrade.holds[].since`); `session_key` is the session ID or '' for a cause of the VM itself. Rows of a
-- cause that ended are deleted.
CREATE TABLE runner.golden_switch_hold (
    vm_id text NOT NULL CHECK (vm_id ~ '^vm-[0-9a-f]{8}$'),
    reason text NOT NULL,
    session_key text NOT NULL,
    since timestamptz NOT NULL,
    PRIMARY KEY (vm_id, reason, session_key)
);
