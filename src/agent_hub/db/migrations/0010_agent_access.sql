-- 0010_agent_access: delegated agent access (KOR-24, ADR-056; 11-agent-access.md section 2).
--
-- `agent` holds one row per pairing of a user's AI agent: its public keys, the state `open` → `confirmed` → `active`
-- (or `revoked`), the times and two ciphertexts the hub cannot open (the user's identity sealed to the agent, the
-- label sealed to the user) plus the hash of the claim the confirming browser handed to the agent over the loopback.
-- An open pairing keeps the source address of its creation (`source`: IPv4, IPv6 by /64; `source_net`: the IP address as text, compared as `inet`)
-- for the per-source and per-network limits; both are cleared at the confirmation and deleted with the row. A user has at most one
-- confirmed or active agent, the main orchestrator (concept idea 34, ADR-056): a passkey confirmation of a new pairing
-- revokes the previous one, and `replaces` names it (`agent_one_per_user` enforces the rule).
-- The login ceremony that confirms a pairing and the browser session it creates carry the pairing ID. Audit rows
-- written while an agent's request is processed carry the agent ID (`audit.agent_id`; the hashed row contains the
-- key only when it is set, so existing hashes stay valid). Every change is additive and the previous hub code starts
-- on this schema, but a rolled-back hub cannot export or restore an org while `hub.agent` exists and cannot verify
-- audit rows that carry an agent ID until it is upgraded again (ADR-056, 11-agent-access.md section 12).

CREATE TABLE hub.agent (
  id           uuid        PRIMARY KEY,
  user_id      uuid        NULL REFERENCES hub.principal (id),
  kem_pub      bytea       NOT NULL CHECK (octet_length(kem_pub) = 32),
  sig_pub      bytea       NOT NULL CHECK (octet_length(sig_pub) = 32),
  state        text        NOT NULL CHECK (state IN ('open', 'confirmed', 'active', 'revoked')),
  bundle_ct    bytea       NULL CHECK (octet_length(bundle_ct) BETWEEN 48 AND 1024),
  label_ct     bytea       NULL CHECK (octet_length(label_ct) BETWEEN 48 AND 1024),
  claim_hash   bytea       NULL CHECK (octet_length(claim_hash) = 32),
  claim_misses integer     NOT NULL DEFAULT 0 CHECK (claim_misses >= 0),
  source       text        NULL CHECK (length(source) <= 64),
  source_net   text        NULL CHECK (source_net::inet = host(source_net::inet)::inet),
  created_at   timestamptz NOT NULL,
  expires_at   timestamptz NOT NULL CHECK (expires_at > created_at),
  confirmed_at timestamptz NULL,
  claimed_at   timestamptz NULL,
  last_used_at timestamptz NULL,
  revoked_at   timestamptz NULL,
  replaces     uuid        NULL,
  CHECK ((state = 'open') = (user_id IS NULL)),
  CHECK (state = 'open' OR (source IS NULL AND source_net IS NULL)),
  CHECK ((state = 'open') = (confirmed_at IS NULL)),
  CHECK (state = 'open' OR (label_ct IS NOT NULL AND claim_hash IS NOT NULL)),
  CHECK ((state IN ('confirmed', 'active')) = (bundle_ct IS NOT NULL)),
  CHECK ((state = 'active') = (claimed_at IS NOT NULL AND revoked_at IS NULL) OR state = 'revoked'),
  CHECK ((state = 'revoked') = (revoked_at IS NOT NULL))
);
CREATE INDEX agent_user ON hub.agent (user_id, created_at DESC) WHERE user_id IS NOT NULL;
CREATE INDEX agent_unclaimed ON hub.agent (expires_at) WHERE state IN ('open', 'confirmed');
CREATE INDEX agent_open_source ON hub.agent (source, expires_at) WHERE state = 'open';
CREATE INDEX agent_open_net ON hub.agent USING gist ((source_net::inet) inet_ops) WHERE state = 'open';
CREATE UNIQUE INDEX agent_sig_pub ON hub.agent (sig_pub) WHERE state <> 'revoked';
CREATE UNIQUE INDEX agent_one_per_user ON hub.agent (user_id) WHERE state IN ('confirmed', 'active');

ALTER TABLE hub.ceremony ADD COLUMN pairing_id uuid NULL;
ALTER TABLE hub.browser_session ADD COLUMN pairing_id uuid NULL;
ALTER TABLE hub.audit ADD COLUMN agent_id uuid NULL;
CREATE INDEX audit_agent ON hub.audit (agent_id, seq DESC) WHERE agent_id IS NOT NULL;

-- Account-level notification of account-changing agent actions (`agent.account_change`, no organization). Both checks
-- are added NOT VALID: every existing row satisfies them (it has an org and an old kind), new and changed rows are
-- checked, and the table is not scanned while the migration holds its lock (decisions KOR-24).
ALTER TABLE hub.notification ALTER COLUMN org_id DROP NOT NULL;
ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'invite.accepted', 'rotation.required', 'agent.account_change'))
  NOT VALID;
ALTER TABLE hub.notification ADD CONSTRAINT notification_org_check
  CHECK ((org_id IS NULL) = (kind = 'agent.account_change')) NOT VALID;

REVOKE ALL ON hub.agent FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.agent TO {writer};
