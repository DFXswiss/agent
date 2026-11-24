-- 0022_github_identities: several GitHub identities per owner, company identities, choices and defaults (KOR-47,
-- ADR-078; 02-data-model.md section 2.7, 04-crypto.md sections 4.4 and 7).
--
-- `credential.owner_kind` says whether the owner is a user (sealed under the user's `CK`) or an organization (a
-- company record sealed under the org key); `created_at` is when the record was first stored (backfilled from
-- `updated_at`); `is_primary` marks the one `github` record of a user that the endpoints from before address (every
-- existing record becomes primary, so nothing changes for anybody; new rows get the value from the hub code). Every
-- other kind stays one record per owner; a user has at most one primary `github` record; a company record is a
-- `github` record and never primary.
-- `github_default` holds the default identity of an organization or a workgroup; it goes away with its record.
-- `task.github_identity`, `session.github_identity` and `operation.github_identity` name the identity chosen for a
-- task, a session (subagents) and the identity an operation acted with; `operation.triggered_by` the human whose
-- instruction it followed; `device_request.org_id` the organization of a `github.connect` for a company identity.
-- These columns have no foreign key: a deleted record must not silently change or drop a choice (the runner answers
-- `github_not_connected` for a choice whose record is gone).

ALTER TABLE hub.credential ADD COLUMN owner_kind text NOT NULL DEFAULT 'user' CHECK (owner_kind IN ('user', 'org'));
ALTER TABLE hub.credential ADD COLUMN created_at timestamptz NULL;
UPDATE hub.credential SET created_at = updated_at;
ALTER TABLE hub.credential ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE hub.credential ADD COLUMN is_primary boolean NOT NULL DEFAULT true;
ALTER TABLE hub.credential ALTER COLUMN is_primary DROP DEFAULT;
ALTER TABLE hub.credential DROP CONSTRAINT credential_owner_id_kind_key;
ALTER TABLE hub.credential ADD CONSTRAINT credential_org_check
  CHECK (owner_kind = 'user' OR (kind = 'github' AND NOT is_primary));
CREATE UNIQUE INDEX credential_owner_kind ON hub.credential (owner_id, kind) WHERE kind <> 'github';
CREATE UNIQUE INDEX credential_github_primary ON hub.credential (owner_id) WHERE kind = 'github' AND is_primary;
CREATE INDEX credential_owner_github ON hub.credential (owner_id, updated_at DESC, id DESC) WHERE kind = 'github';

CREATE TABLE hub.github_default (
  scope       text        NOT NULL CHECK (scope IN ('org', 'wg')),
  scope_id    uuid        NOT NULL,
  identity_id uuid        NOT NULL REFERENCES hub.credential (id) ON DELETE CASCADE,
  set_by      uuid        NOT NULL,
  set_at      timestamptz NOT NULL,
  PRIMARY KEY (scope, scope_id)
);
CREATE INDEX github_default_identity ON hub.github_default (identity_id);

ALTER TABLE hub.task ADD COLUMN github_identity uuid NULL;
ALTER TABLE hub.session ADD COLUMN github_identity uuid NULL;
ALTER TABLE hub.operation ADD COLUMN github_identity uuid NULL;
ALTER TABLE hub.operation ADD COLUMN triggered_by uuid NULL;
ALTER TABLE hub.device_request ADD COLUMN org_id uuid NULL;

REVOKE ALL ON hub.github_default FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.github_default TO {writer};
