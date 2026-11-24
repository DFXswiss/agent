-- 0026_agent_own_identity: the main orchestrator with its own identity (KOR-57, ADR-082; 11-agent-access.md section 2).
--
-- A pairing confirmed in the new form hands over no identity: the row keeps the user's delegation certificate `cert`
-- (the user's Ed25519 signature over the agent's ID, keys, user and generation; public), the generation `gen` the hub
-- counted for the user (one more than the highest of the user's rows) and `user_ct`, the user's public keys and the
-- certificate sealed to the agent under the handover secret (the agent's anchor, in the place of `bundle_ct`).
-- `own_identity` marks such a row (new pairings and converted ones); `held_identity` whether the agent ever held a
-- sealed copy of the user's identity (every row before this migration, a pairing in the old form, a converted one: the
-- honest revocation dialog). `revoked_reason` says why a row was revoked (`revoked` by the user or the agent,
-- `replaced` by a new pairing or recovery, `unclaimed`: an own-identity agent not claimed before its deadline, which is
-- revoked instead of deleted so that its generation stays counted, `session_ended`), `session_id` names the
-- orchestrator session of stage b.
--
-- The check "a confirmed or active row holds `bundle_ct`" becomes "holds `bundle_ct` or `user_ct`". Events may now be
-- authored by an agent (`author_kind` `agent`). Every change is additive: the new columns have defaults or are
-- nullable, existing rows satisfy every check (no row has `own_identity`, `user_ct` or a reason), and the previous hub
-- code starts on this schema; it cannot read an own-identity row (no `bundle_ct`): before rolling the hub back, revoke
-- the own-identity agents (11-agent-access.md section 12).

ALTER TABLE hub.agent ADD COLUMN own_identity boolean NOT NULL DEFAULT false;
ALTER TABLE hub.agent ADD COLUMN held_identity boolean NOT NULL DEFAULT true;
ALTER TABLE hub.agent ADD COLUMN gen integer NULL CHECK (gen >= 1);
ALTER TABLE hub.agent ADD COLUMN cert bytea NULL CHECK (octet_length(cert) = 64);
ALTER TABLE hub.agent ADD COLUMN user_ct bytea NULL CHECK (octet_length(user_ct) BETWEEN 48 AND 1024);
ALTER TABLE hub.agent ADD COLUMN session_id uuid NULL;
ALTER TABLE hub.agent ADD COLUMN revoked_reason text NULL
  CHECK (revoked_reason IN ('revoked', 'replaced', 'unclaimed', 'session_ended'));

DO $$
DECLARE
  doomed text;
BEGIN
  SELECT c.conname INTO STRICT doomed FROM pg_catalog.pg_constraint c
   WHERE c.conrelid = 'hub.agent'::pg_catalog.regclass AND c.contype = 'c'
     AND pg_catalog.pg_get_constraintdef(c.oid) LIKE '%= (bundle_ct IS NOT NULL)%';
  EXECUTE pg_catalog.format('ALTER TABLE hub.agent DROP CONSTRAINT %I', doomed);
END $$;

ALTER TABLE hub.agent ADD CONSTRAINT agent_sealed_check
  CHECK ((state IN ('confirmed', 'active')) = (bundle_ct IS NOT NULL OR user_ct IS NOT NULL));
ALTER TABLE hub.agent ADD CONSTRAINT agent_own_identity_check
  CHECK (own_identity = (cert IS NOT NULL) AND (NOT own_identity OR (gen IS NOT NULL AND bundle_ct IS NULL))
    AND (user_ct IS NULL OR own_identity));
ALTER TABLE hub.agent ADD CONSTRAINT agent_reason_revoked_check CHECK (revoked_reason IS NULL OR state = 'revoked');
CREATE INDEX agent_user_gen ON hub.agent (user_id, gen) WHERE gen IS NOT NULL;

ALTER TABLE hub.event DROP CONSTRAINT event_author_kind_check;
ALTER TABLE hub.event ADD CONSTRAINT event_author_kind_check CHECK (author_kind IN ('user', 'device', 'agent'));
