-- 0003_credential_notice: durable credential-change notices for devices (HUB-13, ADR-035).
--
-- `credential_notice` keeps one row per (device, credential owner, kind) while a device that is eligible for the owner's
-- credential key (04-crypto.md section 6.3 W2, `cred` scope) has not yet re-read the owner's credentials after a
-- change (`PUT`, `DELETE`, CK rotation; 03-api.md section 4.8). It is the reconcile form of the content-free
-- `credentials` hint (with the owner ID): `GET /v1/device/state` lists the owners as `credentials_changed` (03-api.md
-- section 6.1) until the device fetches the owner's record of that kind (`GET /v1/device/credentials/<owner>/<kind>`,
-- also when the answer is `404`). The row holds identifiers, the kind (credential metadata, 04-crypto.md section 7)
-- and a time, never a version or credential content.

CREATE TABLE hub.credential_notice (
  device_id  uuid        NOT NULL,
  owner_id   uuid        NOT NULL,
  kind       text        NOT NULL CHECK (kind IN ('github', 'anthropic_api_key', 'claude_subscription', 'mailbox')),
  changed_at timestamptz NOT NULL,
  PRIMARY KEY (device_id, owner_id, kind)
);
CREATE INDEX credential_notice_owner ON hub.credential_notice (owner_id);

REVOKE ALL ON hub.credential_notice FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.credential_notice TO {writer};
