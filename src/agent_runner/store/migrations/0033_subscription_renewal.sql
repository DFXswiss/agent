-- Renewals of further Claude subscriptions not yet confirmed by the hub (KOR-71, ADR-091 section 2; 02-data-model.md
-- section 4). The provider's answer to a renewal is sealed as the next record version (ciphertext under the owner's
-- `CK`, signed by this device) and stored here before it is written to the hub; the row goes once the hub confirmed
-- the write, a newer login won or the record is gone. `base_record` is the sealed record the renewal replaces (to
-- tell an owner's edit from a newer login after a `version_conflict`). No plaintext token is ever stored.
CREATE TABLE runner.subscription_renewal (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL,
    base_version integer NOT NULL CHECK (base_version > 0),
    base_record jsonb NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
