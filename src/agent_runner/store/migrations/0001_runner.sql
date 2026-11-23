-- Runner store, complete M0 schema (docs/platform/spec/02-data-model.md section 4, ADR-022, ADR-027 section 5).
-- Applied by agent_runner.store.migrate inside one transaction under advisory lock 742180200. Forward-only:
-- this file never changes once released; a later change is a new file with the next number.

CREATE TABLE runner.scope_key (
    scope text NOT NULL CHECK (scope IN ('org', 'wg', 'cred')),
    scope_id uuid NOT NULL,
    epoch integer NOT NULL CHECK (epoch >= 0),
    key_ct bytea NOT NULL,
    source text NOT NULL CHECK (source IN ('package', 'wrap')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, scope_id, epoch)
);

CREATE TABLE runner.log_entry (
    org_id uuid NOT NULL,
    seq bigint NOT NULL CHECK (seq >= 0),
    entry_text text NOT NULL,
    entry_hash text NOT NULL,
    PRIMARY KEY (org_id, seq)
);

CREATE TABLE runner.log_head (
    org_id uuid PRIMARY KEY,
    seq bigint NOT NULL CHECK (seq >= 0),
    hash text NOT NULL
);

CREATE TABLE runner.platform_log_entry (
    seq bigint PRIMARY KEY CHECK (seq >= 0),
    entry_text text NOT NULL,
    entry_hash text NOT NULL
);

-- Gapless origin_seq: writers take pg_advisory_xact_lock(742180201) and insert MAX(origin_seq) + 1 (RUN-02).
CREATE TABLE runner.outbox (
    origin_seq bigint PRIMARY KEY CHECK (origin_seq > 0),
    item_kind text NOT NULL CHECK (
        item_kind IN (
            'event', 'session_transition', 'vm_state', 'operation_transition', 'usage', 'quota', 'metrics',
            'audit', 'wrap'
        )
    ),
    body_ct bytea NOT NULL,
    sealed jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    pushed_at timestamptz
);

CREATE TABLE runner.sync_state (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE runner.inbox_cursor (
    scope_id uuid PRIMARY KEY,
    scope_seq bigint NOT NULL CHECK (scope_seq >= 0)
);

CREATE TABLE runner.golden (
    version text PRIMARY KEY,
    lima_name text NOT NULL UNIQUE,
    claude_version text NOT NULL,
    guest_version text NOT NULL,
    image_digest text NOT NULL,
    built_at timestamptz,
    state text NOT NULL CHECK (state IN ('building', 'ready', 'retired'))
);

CREATE TABLE runner.vm (
    id text PRIMARY KEY CHECK (id ~ '^vm-[0-9a-f]{8}$'),
    lima_name text NOT NULL UNIQUE,
    golden_version text NOT NULL,
    wg_id uuid,
    org_id uuid,
    state text NOT NULL,
    control_token_hash text NOT NULL,
    cpus integer NOT NULL CHECK (cpus > 0),
    memory_mib integer NOT NULL CHECK (memory_mib > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    assigned_at timestamptz,
    last_session_at timestamptz
);

-- One VM per workgroup and device.
CREATE UNIQUE INDEX vm_wg_id_live ON runner.vm (wg_id) WHERE state <> 'deleted';

CREATE TABLE runner.session (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    vm_id text,
    parent_id uuid,
    depth integer NOT NULL DEFAULT 0 CHECK (depth >= 0),
    uid integer,
    state text NOT NULL,
    claude_session_id text,
    model_access text,
    model_id text,
    credential_owner uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    ended_at timestamptz,
    last_prompt_at timestamptz
);

CREATE TABLE runner.prompt_delivery (
    event_id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'registered', 'typed', 'accepted', 'refused', 'failed')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runner.operation (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    task_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    type text NOT NULL,
    state text NOT NULL,
    params jsonb NOT NULL,
    result jsonb,
    error_code text,
    dedupe_tag text,
    attempt integer NOT NULL DEFAULT 1 CHECK (attempt > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runner.guest_cursor (
    vm_id text PRIMARY KEY,
    journal_seq bigint NOT NULL CHECK (journal_seq >= 0)
);

CREATE TABLE runner.credential_cache (
    owner_id uuid NOT NULL,
    kind text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    record jsonb NOT NULL,
    PRIMARY KEY (owner_id, kind)
);

CREATE TABLE runner.device_request (
    id uuid PRIMARY KEY,
    kind text NOT NULL,
    user_id uuid NOT NULL,
    state text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
