-- 0002_outbox_receipt: receipts of the device outbox and nullable quota reset times (HUB-10, ADR-030).
--
-- `outbox_receipt` keeps one row per stored outbox item of a device (03-api.md section 6.1): the SHA-256 of its
-- canonical item text, so that a resent `origin_seq` is recognised as `replayed` (same bytes) or refused as
-- `origin_seq_conflict` (other bytes), and the status it was acknowledged with. It holds no item content.
-- `quota_snapshot.resets_at` becomes nullable: the outbox item `quota` reports `resets_at: null` for windows without
-- a known reset time (contracts/outbox/objects.json `body_quota`).

CREATE TABLE hub.outbox_receipt (
  device_id   uuid        NOT NULL,
  origin_seq  bigint      NOT NULL CHECK (origin_seq >= 1),
  item_kind   text        NOT NULL CHECK (item_kind IN (
                'event', 'session_transition', 'vm_state', 'operation_transition', 'usage', 'quota', 'metrics',
                'audit', 'wrap')),
  item_sha256 bytea       NOT NULL CHECK (octet_length(item_sha256) = 32),
  status      text        NOT NULL CHECK (status IN ('ok', 'rejected')),
  error       text        NULL CHECK (error ~ '^[a-z][a-z0-9_]{{0,63}}$'),
  received_at timestamptz NOT NULL,
  PRIMARY KEY (device_id, origin_seq),
  CHECK ((status = 'rejected') = (error IS NOT NULL))
);
CREATE INDEX outbox_receipt_received ON hub.outbox_receipt (received_at);

ALTER TABLE hub.quota_snapshot ALTER COLUMN resets_at DROP NOT NULL;

REVOKE ALL ON hub.outbox_receipt FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.outbox_receipt TO {writer};
