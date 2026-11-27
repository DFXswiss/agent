-- 0042_dfx_debug: the credential kind dfx_debug (DBG-01, ADR-102 section 2; 02-data-model.md section 2.7).
--
-- A `dfx_debug` record is one user's sign-in at the DFX API debug endpoint (address, signature, API URL and the
-- release to workgroups, all sealed under the user's `CK`); like every single-record kind it is primary and lives in
-- `hub.credential` unchanged. Only the change notices name kinds in a CHECK, so the new kind is added there.
--
-- Every change is additive: the previous hub code starts on this schema and never writes the new kind.

ALTER TABLE hub.credential_notice DROP CONSTRAINT credential_notice_kind_check;
ALTER TABLE hub.credential_notice ADD CONSTRAINT credential_notice_kind_check
  CHECK (kind IN ('github', 'anthropic_api_key', 'claude_subscription', 'mailbox', 'dfx_debug'));
