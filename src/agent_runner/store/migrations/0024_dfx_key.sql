-- DFX model access on own hardware (KOR-50, ADR-081 section 7).
--
-- - `runner.outbox` accepts the items `operator_quota` (ADR-081 section 6), `dfx_key_state`, `dfx_key_issued` and
--   `dfx_key_usage` (section 7).
-- - `runner.dfx_key` (org devices): the envelope of the limited OpenRouter key of each organization this device
--   serves, exactly as the hub delivered it in `dfx_key_held` and only after it was verified (signed by a platform
--   device certified in the verified platform log, naming this device, its KEM key and the organization). The
--   envelope stays sealed to the device's KEM key; it is opened in memory for one provisioning at a time and the
--   plaintext key is never stored.
-- - `runner.dfx_key_issued` (the issuing platform device): the requests this device answered with a key, written in
--   the transaction of the `dfx_key_issued` item, so a request the hub still lists until that item is pushed is
--   never answered twice. IDs and the key reference only; no key.
ALTER TABLE runner.outbox DROP CONSTRAINT outbox_item_kind_check;
ALTER TABLE runner.outbox ADD CONSTRAINT outbox_item_kind_check CHECK (
    item_kind IN (
        'event', 'session_transition', 'vm_state', 'operation_transition', 'usage', 'quota', 'metrics',
        'audit', 'wrap', 'runtime', 'operator_quota', 'dfx_key_state', 'dfx_key_issued', 'dfx_key_usage'
    )
);

CREATE TABLE runner.dfx_key (
    org_id uuid PRIMARY KEY,
    key_ref text NOT NULL CHECK (key_ref ~ '^[A-Za-z0-9_-]{8,128}$'),
    issuer_device_id uuid NOT NULL,
    envelope text NOT NULL CHECK (length(envelope) BETWEEN 1 AND 8192),
    accepted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runner.dfx_key_issued (
    request_id uuid PRIMARY KEY,
    org_id uuid NOT NULL,
    device_id uuid NOT NULL,
    key_ref text NOT NULL CHECK (key_ref ~ '^[A-Za-z0-9_-]{8,128}$'),
    issued_at timestamptz NOT NULL DEFAULT now()
);
