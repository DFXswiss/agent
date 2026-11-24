-- 0014_agent_remote_pairing: pairing an agent that runs on another computer (KOR-42, ADR-062; 11-agent-access.md
-- sections 2 and 5.4).
--
-- A pairing created with `remote: true` keeps `remote`, the hub's 16-byte `nonce` (input of the agent code the user
-- types on the page) and, from the confirmation until the claim, `handover_ct`: the handover secret and the platform
-- root key sealed by the confirming browser to the agent's key, which the hub only relays (`GET /v1/agent/handover`).
-- `handover_ct` is cleared at the claim, at the revocation and at the claim deadline. Every change is additive: the
-- new columns have defaults or are nullable, existing rows are loopback pairings (`remote` false, no nonce, no
-- handover) and satisfy the checks. The previous hub code starts on this schema and ignores the columns; before
-- rolling the hub back, revoke the unclaimed remote rows (11-agent-access.md section 12).

ALTER TABLE hub.agent ADD COLUMN remote boolean NOT NULL DEFAULT false;
ALTER TABLE hub.agent ADD COLUMN nonce bytea NULL;
ALTER TABLE hub.agent ADD COLUMN handover_ct bytea NULL;
ALTER TABLE hub.agent ADD CONSTRAINT agent_nonce_check CHECK (remote = (nonce IS NOT NULL) AND (nonce IS NULL OR
  octet_length(nonce) = 16));
ALTER TABLE hub.agent ADD CONSTRAINT agent_handover_check CHECK (handover_ct IS NULL OR (remote AND state = 'confirmed'
  AND octet_length(handover_ct) BETWEEN 48 AND 1024));
