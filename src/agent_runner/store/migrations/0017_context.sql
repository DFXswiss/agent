-- Context checkpoints and restore (KOR-33, ADR-072 sections 4 and 7; decisions/KOR-33.md).
--
-- `context_session`: the checkpoint bookkeeping of one main session of this device. `due_at` is set when a checkpoint
-- is due (the guest reported idle, suspended, stopped or failed, or `context_interval_s` passed while working) and
-- cleared when it was taken; `archive_sha256` is the SHA-256 of the last archive (an identical archive is not
-- uploaded again; the hash covers only the transcript and workspace members). `attempts`/`next_at` hold the backoff of a
-- failed export. `restored_object_id` names the checkpoint the session started from; `restore_audit` holds the detail
-- of its audit row `session.context_restored` until the guest reports `agent_starting` (then it is sent and cleared);
-- `restore_started` is set once the restored agent ran (`working`, `idle`, `suspended`): its start window is over;
-- `restore_failed` records that its agent did not start from it inside that window (a later restore of the same
-- checkpoint takes the reported emergency path instead of failing again).
CREATE TABLE runner.context_session (
    session_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    due_at timestamptz,
    archive_sha256 text CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    last_at timestamptz,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_at timestamptz,
    restored_object_id uuid,
    restore_audit jsonb,
    restore_started boolean NOT NULL DEFAULT false,
    restore_failed boolean NOT NULL DEFAULT false
);

CREATE INDEX context_session_due ON runner.context_session (due_at) WHERE due_at IS NOT NULL;

-- `context_object`: every encrypted checkpoint in the local cache (`<home>/contexts/<object_id>/`, chunks as uploaded).
-- `source` `local` = taken here (state `sealed` until every chunk and the commit were accepted, then `committed`;
-- `refused` when the hub refused it for good, e.g. `quota_exceeded`, a hub without the route, or an upload the hub
-- expired - the local copy stays and a restore on this device still uses it); `fetched` = downloaded for a restore
-- (always `committed`). Only the manifest's IDs, sizes and digest are stored in clear here; the
-- manifest and signature are those the hub holds as well.
CREATE TABLE runner.context_object (
    object_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    session_id uuid NOT NULL,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    source text NOT NULL CHECK (source IN ('local', 'fetched')),
    state text NOT NULL CHECK (state IN ('sealed', 'committed', 'refused')),
    manifest jsonb NOT NULL,
    sig text NOT NULL,
    bytes bigint NOT NULL CHECK (bytes >= 0),
    uploaded integer NOT NULL DEFAULT 0 CHECK (uploaded >= 0),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_at timestamptz,
    created_at timestamptz NOT NULL,
    committed_at timestamptz
);

CREATE INDEX context_object_task ON runner.context_object (task_id, created_at);
CREATE INDEX context_object_upload ON runner.context_object (next_at) WHERE state = 'sealed';

-- `context_pr_link`: pull requests of this device's `github.create_pull_request` operations registered at the hub
-- (ADR-072 section 8). A row exists once the registration was accepted (`done`) or refused for good (`refused`);
-- `attempts`/`next_at` hold the backoff while the hub cannot be reached.
CREATE TABLE runner.context_pr_link (
    operation_id uuid PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('pending', 'done', 'refused')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
