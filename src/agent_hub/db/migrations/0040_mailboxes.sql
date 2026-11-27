-- 0040_mailboxes: several mailboxes per user, each released to workgroups whose sessions read it with mail.find
-- (KOR-74, ADR-099 sections 2 and 3; 02-data-model.md section 2.7).
--
-- A user may hold several `mailbox` records like several `github` records (ADR-078); the primary one stays the mailbox
-- of the automatic login that the endpoints from before address. Existing rows are all primary already, so nothing
-- changes for anybody.
-- `mailbox_use` is a plaintext mirror of the release sealed in the record: the workgroup IDs (a JSON array as the
-- export file writes it). The hub uses it only to answer a device the mailboxes of one workgroup; the runner acts on
-- the sealed release only, so a mirror that names more than the record allows nothing. It goes with its record; a
-- record without a row is released nowhere.

ALTER TABLE hub.credential DROP CONSTRAINT credential_primary_check;
ALTER TABLE hub.credential ADD CONSTRAINT credential_primary_check
  CHECK (is_primary OR kind IN ('github', 'claude_subscription', 'mailbox'));
CREATE INDEX credential_owner_mailbox ON hub.credential (owner_id, updated_at DESC, id DESC)
  WHERE kind = 'mailbox';

CREATE TABLE hub.mailbox_use (
  credential_id uuid        PRIMARY KEY REFERENCES hub.credential (id) ON DELETE CASCADE,
  owner_id      uuid        NOT NULL,
  wgs           jsonb       NOT NULL,
  set_at        timestamptz NOT NULL,
  CHECK (jsonb_typeof(wgs) = 'array' AND jsonb_array_length(wgs) > 0)
);
CREATE INDEX mailbox_use_wgs ON hub.mailbox_use USING gin (wgs);

REVOKE ALL ON hub.mailbox_use FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.mailbox_use TO {writer};
