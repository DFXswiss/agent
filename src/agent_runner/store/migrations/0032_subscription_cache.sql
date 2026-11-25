-- Claude subscriptions (KOR-69, ADR-090; 02-data-model.md section 4).
-- `subscription_cache`: subscription records fetched by ID (`GET /v1/device/subscriptions/{id}`), kept exactly as the
-- hub stores them (header, ciphertext, signature) with their owner, the hub's primary mark and the verified version;
-- the body is opened only into memory. The primary record also stays in `credential_cache` (the legacy endpoints
-- address it).
CREATE TABLE runner.subscription_cache (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    is_primary boolean NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    record jsonb NOT NULL
);

CREATE INDEX subscription_cache_owner ON runner.subscription_cache (owner_id);
