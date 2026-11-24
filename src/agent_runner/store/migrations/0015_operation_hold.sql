-- Held operations (KOR-25): a `github.*` operation whose formal check or pre-check fails only because the task owner
-- has no usable GitHub connection (`github_not_connected`) stays `received` and is checked again until the owner
-- connects or `held_until` (the request plus 15 min) passes. `held_reason` is the waiting reason of the
-- `session.status` event (`github_connection`), `held_repo` the checked repository it names, `held_notified_at` when
-- that event was written for the current reason (device wall clock), `held_ended_at` when the hold ended (the
-- registration's 10 min count from there), `held_credential` the version of the owner's cached `github` credential
-- the last check refused (checked again only once another version arrives, so a revoked token is not sent to GitHub
-- at every scan).
ALTER TABLE runner.operation
    ADD COLUMN held_reason text CHECK (held_reason IN ('github_connection')),
    ADD COLUMN held_repo text,
    ADD COLUMN held_until timestamptz,
    ADD COLUMN held_notified_at timestamptz,
    ADD COLUMN held_ended_at timestamptz,
    ADD COLUMN held_credential integer;

CREATE INDEX operation_held ON runner.operation (session_id, held_reason) WHERE held_until IS NOT NULL;
