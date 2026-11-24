-- 0023_no_approval: switches of the platform log, the platform role `operator` and the requests to operators and admins
-- (KOR-49, ADR-079; 02-data-model.md sections 2.1, 2.2 and 2.6).
--
-- `platform_policy` projects the latest `platform_policy` entry of each switch (`agent_hub.platform.projection`); a
-- switch without a row is off. `org.platform_access_grant` keeps the latest `org_platform_access` of the org (NULL:
-- none), so `org.platform_access` = `private OR COALESCE(platform_access_grant, <switch open_platform_access>)` can be
-- derived again when the switch changes. `operator` is a new platform role (the requests of ADR-079 section 4 go to
-- its holders). `operator.request` (account level without an org, to operators) and `device.needed` (to the org's
-- admins) are new notification kinds; the checks are replaced NOT VALID like in 0010, 0013 and 0021.

CREATE TABLE hub.platform_policy (
  name text    PRIMARY KEY CHECK (name IN ('open_platform_access', 'open_platform_invites')),
  "on" boolean NOT NULL,
  seq  integer NOT NULL CHECK (seq >= 0)
);

ALTER TABLE hub.org ADD COLUMN platform_access_grant boolean NULL;
UPDATE hub.org o SET platform_access_grant = (e.entry_text::jsonb -> 'body' ->> 'granted')::boolean
  FROM (SELECT DISTINCT ON (subject_id) subject_id, entry_text FROM hub.platform_entry
        WHERE type = 'org_platform_access' ORDER BY subject_id, seq DESC) e
  WHERE e.subject_id = o.id;

ALTER TABLE hub.platform_role DROP CONSTRAINT platform_role_role_check;
ALTER TABLE hub.platform_role ADD CONSTRAINT platform_role_role_check CHECK (role IN ('inviter', 'operator'));

ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'task.attention', 'invite.accepted', 'rotation.required',
  'agent.account_change', 'org.admitted', 'operator.request', 'device.needed'))
  NOT VALID;
ALTER TABLE hub.notification DROP CONSTRAINT notification_org_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_org_check
  CHECK ((org_id IS NULL) = (kind IN ('agent.account_change', 'operator.request'))) NOT VALID;

REVOKE ALL ON hub.platform_policy FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.platform_policy TO {writer};
