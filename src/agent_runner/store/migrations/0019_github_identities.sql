-- GitHub identities (KOR-47, ADR-076; 02-data-model.md section 4).
-- `github_identity_cache`: identity records fetched by ID (`GET /v1/device/github-identities/{id}`), kept exactly as
-- the hub stores them (header, ciphertext, signature) with their owner (a user or an organization), the hub's primary
-- mark and the verified version; the body is opened only into memory. A user's primary record stays in
-- `credential_cache` as before (the legacy endpoints address it).
-- `github_selection`: the last desired state's GitHub identity of each session and of its task;
-- `github_default`: the defaults of the organizations and workgroups the device is assigned to. Both keep the hub's
-- last answer, so a restart without the hub never acts with another account (ADR-076 section 3).
CREATE TABLE runner.github_identity_cache (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    owner_kind text NOT NULL CHECK (owner_kind IN ('user', 'org')),
    is_primary boolean NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    record jsonb NOT NULL
);

CREATE INDEX github_identity_cache_owner ON runner.github_identity_cache (owner_id);

CREATE TABLE runner.github_selection (
    session_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    github_identity uuid,
    task_github_identity uuid
);

CREATE INDEX github_selection_task ON runner.github_selection (task_id);

CREATE TABLE runner.github_default (
    scope text NOT NULL CHECK (scope IN ('org', 'wg')),
    scope_id uuid NOT NULL,
    identity_id uuid NOT NULL,
    PRIMARY KEY (scope, scope_id)
);
