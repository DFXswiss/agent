-- 0008_ceremony_recovery_rotate: the ceremony that proves the user before the recovery code is replaced (KOR-01,
-- AUD-01 B-06; 06-web.md section 3.2 "generate a new recovery code").
--
-- `POST /v1/me/recovery/begin` stores a `recovery_rotate` ceremony of the signed-in user (32-byte challenge, 300 s,
-- used once); `PUT /v1/me/bundles/recovery` with a passkey assertion over that challenge replaces the recovery slot
-- (`agent_hub.me.recovery_code`). Like every ceremony it is purged one day after its creation.

ALTER TABLE hub.ceremony DROP CONSTRAINT ceremony_kind_check;
ALTER TABLE hub.ceremony ADD CONSTRAINT ceremony_kind_check
  CHECK (kind IN ('register', 'login', 'add_passkey', 'recovery', 'recovery_rotate'));
