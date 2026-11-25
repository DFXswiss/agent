-- The main orchestrator in a session of this device (KOR-57, ADR-082 sections 6 and 9.6-9.8; 05-runner.md section 17).
--
-- `orchestrator`: one row per session whose keys the device registered at the hub as the user's main orchestrator.
-- `pairing_id` is the pairing the guest's tool drew (a new registration of the same session replaces it); `state` is
-- `registered` until the runner passed the checked handover into the VM (`handed_over`), `ended` once the hub listed
-- the pairing as revoked while the session goes on. Only a `registered` or `handed_over` row may use the hub relay; a
-- session with a row of any state never becomes due for a context checkpoint (section 17.6) nor is suspended when idle;
-- the row is deleted when the session ends.
CREATE TABLE runner.orchestrator (
    session_id uuid PRIMARY KEY,
    pairing_id uuid NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('registered', 'handed_over', 'ended')),
    registered_at timestamptz NOT NULL,
    handed_over_at timestamptz,
    CHECK (state <> 'handed_over' OR handed_over_at IS NOT NULL)
);
