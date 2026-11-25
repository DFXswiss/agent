-- 0032_orchestrator_session: the main orchestrator on platform hardware (KOR-57 stage b, ADR-082 sections 6 and 9.6-9.9;
-- 11-agent-access.md section 11).
--
-- `session.orchestrator_at` records the task owner's request "make this session my main orchestrator"
-- (`POST /v1/sessions/<id>/orchestrator`); the runner of the session reads it and registers the keys the session drew
-- in its VM as an ordinary pairing (`hub.agent`) bound to the session (`session_id`, exists since 0026), its device
-- (`device_id`) and, through the session, its task owner, with `device_sig`, the device's Ed25519 signature over the
-- fingerprint of those keys. After the confirmation the browser stores the handover sealed to the session's key in
-- `handover_ct` with `handover_sig`, the task owner's signature over it, which the runner checks before it passes the
-- handover into the VM; both are cleared at the claim, the revocation and the claim deadline. A session pairing is no
-- remote pairing (no nonce), so the handover check now admits `handover_ct` for a remote or a session pairing.
--
-- Every change is additive: new columns are nullable, existing rows have no session and satisfy every check. The
-- previous hub code starts on this schema and ignores the columns; before rolling the hub back, revoke the session
-- orchestrators (11-agent-access.md section 12).

ALTER TABLE hub.session ADD COLUMN orchestrator_at timestamptz NULL;

ALTER TABLE hub.agent ADD COLUMN device_id uuid NULL;
ALTER TABLE hub.agent ADD COLUMN device_sig bytea NULL CHECK (octet_length(device_sig) = 64);
ALTER TABLE hub.agent ADD COLUMN handover_sig bytea NULL CHECK (octet_length(handover_sig) = 64);
ALTER TABLE hub.agent ADD CONSTRAINT agent_session_check
  CHECK ((session_id IS NULL) = (device_id IS NULL) AND (session_id IS NULL) = (device_sig IS NULL)
    AND NOT (remote AND session_id IS NOT NULL));
ALTER TABLE hub.agent ADD CONSTRAINT agent_handover_signed_check
  CHECK ((handover_sig IS NULL OR (session_id IS NOT NULL AND handover_ct IS NOT NULL))
    AND (session_id IS NULL OR (handover_ct IS NULL) = (handover_sig IS NULL)));
ALTER TABLE hub.agent DROP CONSTRAINT agent_handover_check;
ALTER TABLE hub.agent ADD CONSTRAINT agent_handover_check CHECK (handover_ct IS NULL
  OR ((remote OR session_id IS NOT NULL) AND state = 'confirmed' AND octet_length(handover_ct) BETWEEN 48 AND 1024));
CREATE INDEX agent_session ON hub.agent (session_id, created_at DESC) WHERE session_id IS NOT NULL;
