-- Model usage of a session merged over 10 s before it becomes one outbox item `usage` (KOR-35, ADR-086 section 3;
-- 05-runner.md section 10.4; `model/merge.py`). One row per session: the merged outbox body (session ID, counters, cost
-- basis, context figures, provider - no content) and the device wall clock of its first delta in seconds.
CREATE TABLE runner.usage_pending (
    session_id uuid PRIMARY KEY,
    item jsonb NOT NULL,
    first_wall double precision NOT NULL
);
