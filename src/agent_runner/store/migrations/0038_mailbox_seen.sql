-- Mailboxes read by sessions (KOR-74, ADR-099 section 3; 02-data-model.md section 4).
-- `mailbox_seen`: the newest version of every mailbox record this device verified for `mail.find`, so a hub that
-- serves an older version (one that still released a workgroup) is refused - no rollback, like `credential_cache`.
-- Only IDs and versions; the record itself is never stored.
CREATE TABLE runner.mailbox_seen (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    version integer NOT NULL CHECK (version > 0)
);
