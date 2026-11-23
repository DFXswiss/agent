-- Metric samples waiting for the hub (RUN-15, decisions/RUN-15.md item 17; 08-monitoring.md section 2).
-- One row per sampling round of the `metrics` task. Rows move into the outbox (item `metrics`) while the device is
-- connected and the backlog is small; rows older than 48 h are deleted (counted as dropped) before they could be
-- pushed, which the gapless outbox cannot do. The samples carry only IDs and numbers the hub receives in plaintext.
CREATE TABLE runner.metric_pending (
    id bigserial PRIMARY KEY,
    taken_at timestamptz NOT NULL,
    samples jsonb NOT NULL CHECK (jsonb_typeof(samples) = 'array')
);
CREATE INDEX metric_pending_taken ON runner.metric_pending (taken_at);
