-- 0036_agent_profiles: rights profiles of sessions and agent roles (KOR-51, ADR-094; 02-data-model.md section 2).
--
-- `agent_profile` holds the own profiles of an organization (`scope` 'org', `scope_id` = the org) or of a workgroup
-- (`scope` 'wg', `scope_id` = the workgroup): a name, the operation types it allows and the reach of its reads. The
-- built-in roles are code, never rows. Names and type lists are configuration, no content of a task.
-- `task.agent_profile` (for the task's main sessions) and `session.agent_profile` (a child's own) store the resolved
-- snapshot (name, operations, reads) taken when the profile was assigned; NULL = no profile = every right (A1, E-11).
-- Nothing sets them by default.

CREATE TABLE hub.agent_profile (
  scope      text        NOT NULL CHECK (scope IN ('org', 'wg')),
  scope_id   uuid        NOT NULL,
  org_id     uuid        NOT NULL REFERENCES hub.org (id),
  name       text        NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{{0,39}}$'),
  operations text[]      NOT NULL,
  reads      text        NOT NULL CHECK (reads IN ('all', 'workgroup', 'task')),
  updated_by uuid        NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (scope, scope_id, name)
);
CREATE INDEX agent_profile_org ON hub.agent_profile (org_id);

ALTER TABLE hub.task ADD COLUMN agent_profile jsonb NULL;
ALTER TABLE hub.session ADD COLUMN agent_profile jsonb NULL;

REVOKE ALL ON hub.agent_profile FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.agent_profile TO {writer};
