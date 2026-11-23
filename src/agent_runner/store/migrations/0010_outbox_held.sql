-- Scope queues of the outbox (KOR-08, ADR-046; AUD-01 A-01). An event the device cannot seal yet (no key of the
-- needed epoch, no verified log head) waits here together with the later items of its scope, outside the gapless
-- `origin_seq` numbering, so it holds back only its own scope. Items get their number when the scope can be sealed
-- again (`sync.outbox.release`), or are discarded with an audit once the verified log shows that the device may read
-- the scope no more (`sync.reconcile`). `event_id` is the event's ID (`event`) or the `result_event_id` an
-- `operation_transition` waits for. Bodies are sealed with the storage key like `runner.outbox.body_ct` (context
-- `held-<id>:<item_kind>`); `scope_id` and `event_id` are IDs only.
CREATE TABLE runner.outbox_held (
    id bigserial PRIMARY KEY,
    scope text NOT NULL CHECK (scope IN ('org', 'wg')),
    scope_id uuid NOT NULL,
    item_kind text NOT NULL CHECK (item_kind IN ('event', 'operation_transition')),
    event_id uuid NOT NULL,
    body_ct bytea NOT NULL,
    sealed jsonb,
    min_epoch integer NOT NULL DEFAULT 1 CHECK (min_epoch >= 1),
    created_at timestamptz NOT NULL,
    held_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX outbox_held_scope ON runner.outbox_held (scope, scope_id, id);
CREATE INDEX outbox_held_event ON runner.outbox_held (event_id);
