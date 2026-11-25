-- 0034_subscription_use: which sessions besides the owner's may use a further Claude subscription, as the owner's client
-- tells the hub (KOR-69, ADR-090 section 3; 02-data-model.md section 2.7).
--
-- A plaintext mirror of the `use` sealed in the record: `pool` (the operator's pool) and `orgs` (organization IDs, a
-- JSON array as the export file writes it). The
-- hub uses it only to list the subscription in the device state of the devices serving those organizations; the
-- runner acts on the sealed `use` only, so a mirror that names more than the record allows nothing. It goes with its
-- record; a record without a row is measured only.

CREATE TABLE hub.subscription_use (
  credential_id uuid        PRIMARY KEY REFERENCES hub.credential (id) ON DELETE CASCADE,
  owner_id      uuid        NOT NULL,
  pool          boolean     NOT NULL,
  orgs          jsonb       NOT NULL,
  set_at        timestamptz NOT NULL,
  CHECK (jsonb_typeof(orgs) = 'array' AND (pool OR jsonb_array_length(orgs) > 0))
);
CREATE INDEX subscription_use_orgs ON hub.subscription_use USING gin (orgs);

REVOKE ALL ON hub.subscription_use FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.subscription_use TO {writer};
