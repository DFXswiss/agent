-- 0033_subscriptions: several Claude subscriptions per user, each with its own reading (KOR-69, ADR-090; 02-data-model.md
-- sections 2.7 and 2.8).
--
-- A user may hold several `claude_subscription` records like several `github` records (ADR-078); at most one record of
-- each kind and owner is primary, and every record of the other kinds is primary (the endpoints from before address the
-- primary record only). Existing rows are all primary already (0022), so nothing changes for anybody.
-- `subscription_quota` keeps the newest reading of every non-primary subscription (the primary one keeps
-- `quota_snapshot` and `quota_unreadable` of its owner): the windows as the device sent them, or `unreadable` when the
-- usage endpoint refused the token. It goes with its record.

DROP INDEX hub.credential_owner_kind;
DROP INDEX hub.credential_github_primary;
ALTER TABLE hub.credential ADD CONSTRAINT credential_primary_check
  CHECK (is_primary OR kind IN ('github', 'claude_subscription'));
CREATE UNIQUE INDEX credential_owner_primary ON hub.credential (owner_id, kind) WHERE is_primary;
CREATE INDEX credential_owner_subscription ON hub.credential (owner_id, updated_at DESC, id DESC)
  WHERE kind = 'claude_subscription';

CREATE TABLE hub.subscription_quota (
  credential_id uuid        PRIMARY KEY REFERENCES hub.credential (id) ON DELETE CASCADE,
  owner_id      uuid        NOT NULL,
  windows       jsonb       NULL,
  unreadable    boolean     NOT NULL,
  fetched_at    timestamptz NOT NULL,
  device_id     uuid        NOT NULL,
  CHECK (unreadable = (windows IS NULL))
);
CREATE INDEX subscription_quota_owner ON hub.subscription_quota (owner_id);

REVOKE ALL ON hub.subscription_quota FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.subscription_quota TO {writer};
