-- 0012_session_context: encrypted context checkpoints of main sessions, opaque pull-request links and the restore
-- outcome on the session row (KOR-33, ADR-059 sections 5-8; 02-data-model.md section 2.4).
--
-- `context_object` is one checkpoint of a task: uploaded chunk by chunk (`uploading`, `chunks`/`size` count what is
-- stored so far, `updated_at` = the last chunk) and sealed by the commit (`complete`: the signed manifest, its epoch and
-- digest). The hub holds no key: chunks are AES-GCM ciphertext under a key derived from the workgroup key
-- (04-crypto.md section 4.11). A checkpoint is deleted with its task (cascade), when newer complete checkpoints of the
-- task exceed `AGENT_HUB_CONTEXT_KEEP`, and an upload without a chunk for 6 h by the job `contexts.expire`.
-- `pr_link` maps an HMAC tag of a pull request (repository and number never reach the hub) to the task and session
-- that created it. `session.context_restore`/`context_reason` record what the runner reported for a new main session:
-- the checkpoint was restored (with `workspace_partial`/`workspace_omitted` when the workspace did not come back
-- whole), or the session continued with a new conversation (the reported emergency path, with its reason).

CREATE TABLE hub.context_object (
  id           uuid        PRIMARY KEY,
  org_id       uuid        NOT NULL,
  wg_id        uuid        NOT NULL,
  task_id      uuid        NOT NULL REFERENCES hub.task (id) ON DELETE CASCADE,
  session_id   uuid        NOT NULL,
  author_id    uuid        NOT NULL,
  state        text        NOT NULL CHECK (state IN ('uploading', 'complete')),
  chunks       integer     NOT NULL DEFAULT 0 CHECK (chunks >= 0),
  size         bigint      NOT NULL DEFAULT 0 CHECK (size >= 0),
  epoch        integer     NULL CHECK (epoch >= 1),
  digest       text        NULL,
  manifest_text text       NULL,
  sig          bytea       NULL,
  created_at   timestamptz NOT NULL,
  updated_at   timestamptz NOT NULL,
  completed_at timestamptz NULL,
  CHECK ((state = 'complete') = (completed_at IS NOT NULL)),
  CHECK (state = 'uploading' OR (epoch IS NOT NULL AND digest IS NOT NULL AND manifest_text IS NOT NULL
    AND sig IS NOT NULL))
);
CREATE INDEX context_object_task ON hub.context_object (task_id, session_id, id DESC) WHERE state = 'complete';
CREATE INDEX context_object_org ON hub.context_object (org_id);
CREATE INDEX context_object_stale ON hub.context_object (updated_at) WHERE state = 'uploading';

CREATE TABLE hub.context_chunk (
  object_id   uuid    NOT NULL REFERENCES hub.context_object (id) ON DELETE CASCADE,
  chunk_index integer NOT NULL CHECK (chunk_index >= 0),
  ct          bytea   NOT NULL,
  sha256      bytea   NOT NULL,
  PRIMARY KEY (object_id, chunk_index)
);

CREATE TABLE hub.pr_link (
  wg_id      uuid        NOT NULL,
  epoch      integer     NOT NULL CHECK (epoch >= 1),
  tag        text        NOT NULL,
  task_id    uuid        NOT NULL REFERENCES hub.task (id) ON DELETE CASCADE,
  session_id uuid        NOT NULL,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (wg_id, epoch, tag)
);
CREATE INDEX pr_link_task ON hub.pr_link (task_id);

ALTER TABLE hub.session ADD COLUMN context_restore text NULL CHECK (context_restore IN ('restored', 'fallback'));
ALTER TABLE hub.session ADD COLUMN context_reason text NULL CHECK (context_reason IN (
  'context_missing', 'context_invalid', 'context_unavailable', 'version_incompatible', 'restore_failed',
  'workspace_partial', 'workspace_omitted'));
-- fallback: always one of the fallback reasons; restored: none, or what was missing of the workspace; nothing: none.
ALTER TABLE hub.session ADD CONSTRAINT session_context_reason CHECK (CASE context_restore
  WHEN 'fallback' THEN context_reason IS NOT NULL AND context_reason IN (
    'context_missing', 'context_invalid', 'context_unavailable', 'version_incompatible', 'restore_failed')
  WHEN 'restored' THEN context_reason IS NULL OR context_reason IN ('workspace_partial', 'workspace_omitted')
  ELSE context_reason IS NULL END);

REVOKE ALL ON hub.context_object, hub.context_chunk, hub.pr_link FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.context_object, hub.context_chunk, hub.pr_link TO {writer};
