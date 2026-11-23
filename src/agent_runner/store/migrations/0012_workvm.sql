-- Work VMs on request (KOR-29, ADR-059; docs/platform/spec/13-work-vm.md section 8). One row per work VM the runner
-- created: its own random ID (`wvm-<8 hex>`), the Lima name, the slot (the macOS account index that runs it), the task
-- (scope `task`) or session (scope `session`) it belongs to, size and state. A task has at most one live task VM and a
-- session at most one live session VM (partial unique indexes), a slot at most one live VM.
-- `workvm_mirror` keeps, per work VM and session, the SHA-256 manifest of the workspace files last mirrored into the VM
-- (only paths and hashes, no content). No destination or traffic log exists (addendum 1 of ADR-059). The runtime of a
-- work VM (KONZEPT idea 46) is `deleted_at - created_at` of its row, attributed by task, session, workgroup and
-- organization; VMs are temporary (addendum 2): every row ends `deleted` with a reason.
CREATE TABLE runner.workvm (
    id text PRIMARY KEY CHECK (id ~ '^wvm-[0-9a-f]{8}$'),
    lima_name text NOT NULL UNIQUE,
    slot integer NOT NULL CHECK (slot >= 0 AND slot < 100),
    scope text NOT NULL CHECK (scope IN ('task', 'session')),
    task_id uuid NOT NULL,
    session_id uuid,
    requested_by uuid NOT NULL,
    implicit boolean NOT NULL DEFAULT false,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    owner_id uuid,
    size text NOT NULL CHECK (size IN ('standard', 'large', 'xlarge')),
    cpus integer NOT NULL CHECK (cpus > 0),
    memory_mib integer NOT NULL CHECK (memory_mib > 0),
    disk_gib integer NOT NULL CHECK (disk_gib > 0),
    image text NOT NULL,
    state text NOT NULL CHECK (state IN ('starting', 'running', 'stopping', 'stopped', 'deleting', 'deleted')),
    created_at timestamptz NOT NULL,
    last_used_at timestamptz NOT NULL,
    stopped_at timestamptz,
    deleted_at timestamptz,
    delete_reason text CHECK (delete_reason IN ('released', 'idle', 'session_ended', 'task_closed', 'failed', 'restart')),
    CHECK ((scope = 'session') = (session_id IS NOT NULL))
);
CREATE UNIQUE INDEX workvm_task_live ON runner.workvm (task_id) WHERE scope = 'task' AND state <> 'deleted';
CREATE UNIQUE INDEX workvm_session_live ON runner.workvm (session_id) WHERE scope = 'session' AND state <> 'deleted';
CREATE UNIQUE INDEX workvm_slot_live ON runner.workvm (slot) WHERE state <> 'deleted';

CREATE TABLE runner.workvm_mirror (
    workvm_id text NOT NULL REFERENCES runner.workvm (id) ON DELETE CASCADE,
    session_id uuid NOT NULL,
    root text NOT NULL,
    manifest jsonb NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (workvm_id, session_id, root)
);

CREATE TABLE runner.workvm_image (
    name text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('building', 'ready', 'retired')),
    built_at timestamptz NOT NULL
);

-- Warm pool (stage b, spec 11 section 7): started, unassigned standard work VMs. A request takes the oldest `ready`
-- entry of the current image; the entry becomes a `runner.workvm` row with the same Lima name and slot in one
-- transaction. A pool VM holds its slot and RAM; it is never resumed after a restart (deleted by `reconcile`).
CREATE TABLE runner.workvm_pool (
    lima_name text PRIMARY KEY,
    slot integer NOT NULL UNIQUE CHECK (slot >= 0 AND slot < 100),
    image text NOT NULL,
    memory_mib integer NOT NULL CHECK (memory_mib > 0),
    state text NOT NULL CHECK (state IN ('starting', 'ready')),
    created_at timestamptz NOT NULL,
    ready_at timestamptz
);
