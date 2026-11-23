-- Content-free session pings (RUN-14, decisions/RUN-14.md; 05-runner.md section 9, ADR-020). One row per ping this
-- device hands to a guest: the durable `pings` of `GET /v1/device/state` (hub `session_ping`, `whiteboard.entry`) and
-- the runner's own `subagent.finished` / `operation.finished`. `sent_at` is set once the guest kept the ping (rows
-- without it are sent again by the reconcile step); `delivered_at` once the guest journaled `ping.delivered`, which
-- writes the outbox audit `ping.delivered` for a hub ping. Rows of sessions that ended an hour ago are deleted.
CREATE TABLE runner.ping (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('whiteboard.entry', 'subagent.finished', 'operation.finished')),
    level text CHECK (level IN ('org', 'workgroup', 'task')),
    ref uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    delivered_at timestamptz,
    CHECK ((kind = 'whiteboard.entry') = (level IS NOT NULL))
);
CREATE INDEX ping_unsent ON runner.ping (session_id) WHERE sent_at IS NULL;
