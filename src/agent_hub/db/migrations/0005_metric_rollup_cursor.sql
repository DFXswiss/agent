-- 0005_metric_rollup_cursor: what the 5-minute rollup of the metric samples has already seen (HUB-16).
--
-- Samples reach the hub late (the runner's outbox queues them during outages, 08-monitoring.md section 2), so the
-- rollup job cannot only look at the newest buckets. `metric_sample.received_at` is the time the hub stored a sample
-- (the default of the column: the outbox writes it in its transaction), and `metric_rollup_cursor` keeps the time up
-- to which the job has rolled up every received sample. A run recomputes exactly the buckets touched by samples
-- received since then (with an overlap for transactions that committed late), so it is idempotent and catches up
-- after the hub was down.

ALTER TABLE hub.metric_sample ADD COLUMN received_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX metric_sample_received ON hub.metric_sample (received_at);

CREATE TABLE hub.metric_rollup_cursor (
  id               smallint    PRIMARY KEY CHECK (id = 1),
  received_through timestamptz NOT NULL
);
INSERT INTO hub.metric_rollup_cursor (id, received_through) VALUES (1, '1970-01-01T00:00:00Z');

REVOKE ALL ON hub.metric_rollup_cursor FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.metric_rollup_cursor TO {writer};
