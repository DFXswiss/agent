-- The preparation of a merge commit (KOR-48, ADR-084 section 4; `github.merge_base`, `github.commit` with `merge`).
-- One row per session and repository (lower case): the newest preparation; a later checkout or preparation of the
-- repository in the session replaces it. `changed` is the merge result for every path the merge changed against the
-- head (`{path: [mode, sha] | null}`), `conflicts` every listed conflict (`{path: kind}`), `sides` the head's and the
-- base's entry of every conflict without markers (`{path: {"head": entry, "base": entry}}`), `markers` the
-- marker-like lines the head's and base's versions of every `content` conflict already contain (`{path: [line]}`).
-- `workspace_state` holds the digest of what the runner last put into or read from a repository directory of a
-- session's workspace (checkout, preparation, the snapshot of a succeeded commit with its `commit_sha`
-- and `branch`): the workspace
-- protection of `github.merge_base`.
CREATE TABLE runner.merge_prep (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    repo text NOT NULL,
    number integer NOT NULL CHECK (number > 0),
    branch text NOT NULL,
    head_sha text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    base_sha text NOT NULL CHECK (base_sha ~ '^[0-9a-f]{40}$'),
    merge_base_sha text NOT NULL CHECK (merge_base_sha ~ '^[0-9a-f]{40}$'),
    changed jsonb NOT NULL,
    conflicts jsonb NOT NULL,
    sides jsonb NOT NULL,
    markers jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'committed', 'pushed')),
    commit_sha text NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, repo)
);
CREATE INDEX merge_prep_created ON runner.merge_prep (created_at);

CREATE TABLE runner.workspace_state (
    session_id uuid NOT NULL,
    dir text NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    commit_sha text NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    branch text NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, dir)
);
CREATE INDEX workspace_state_updated ON runner.workspace_state (updated_at);
