-- 0038_support: the release of workgroups to DFX support (KOR-56, ADR-096; 02-data-model.md sections 2.1, 2.2, 2.6).
--
-- `support_person` projects the support roster of the verified platform log (`agent_hub.support.store.sync_people`:
-- a user with the platform role `support` bound to its valid certification is `active`); `name` is the display name
-- the operator sets with a root-signed request (`PUT /v1/platform/support/<user>`), the only clear name the hub
-- keeps of a support person (staff of the operator, not customer content). `support_release` projects the active
-- `support_grant` of each workgroup from the membership log (`agent_hub.orgs.projection`, rebuilt with it);
-- `support_access` records the first access of a person per release (the notification "first access" is sent once).
-- `support` joins the platform roles; `support.release`, `support.access` and `support.people` are new notification
-- kinds (the checks are replaced NOT VALID like in 0023 and 0027).
--
-- Every change is additive: the previous hub code starts on this schema and ignores the new tables.

CREATE TABLE hub.support_person (
  user_id    uuid        PRIMARY KEY,
  active     boolean     NOT NULL,
  name       text        NULL CHECK (name IS NULL OR char_length(name) BETWEEN 1 AND 64),
  changed_at timestamptz NOT NULL
);

CREATE TABLE hub.support_release (
  wg_id       uuid        PRIMARY KEY,
  org_id      uuid        NOT NULL,
  access      text        NOT NULL CHECK (access IN ('read', 'write')),
  granted_seq integer     NOT NULL CHECK (granted_seq >= 0),
  granted_at  timestamptz NOT NULL
);
CREATE INDEX support_release_org ON hub.support_release (org_id);

CREATE TABLE hub.support_access (
  wg_id       uuid        NOT NULL,
  user_id     uuid        NOT NULL,
  granted_seq integer     NOT NULL CHECK (granted_seq >= 0),
  first_at    timestamptz NOT NULL,
  PRIMARY KEY (wg_id, user_id, granted_seq)
);

ALTER TABLE hub.platform_role DROP CONSTRAINT platform_role_role_check;
ALTER TABLE hub.platform_role ADD CONSTRAINT platform_role_role_check
  CHECK (role IN ('inviter', 'operator', 'support'));

ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'task.attention', 'invite.accepted', 'rotation.required',
  'agent.account_change', 'org.admitted', 'operator.request', 'device.needed', 'model.switched',
  'prompt.waiting_for_device', 'support.release', 'support.access', 'support.people'))
  NOT VALID;

REVOKE ALL ON hub.support_person, hub.support_release, hub.support_access FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.support_person, hub.support_release, hub.support_access TO {writer};
