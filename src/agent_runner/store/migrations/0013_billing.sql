-- Usage billing (KOR-31, ADR-062 sections 3 and 6; 12-billing.md sections 4 and 7).
--
-- - `runner.outbox` accepts the item `runtime` (VM runtime of this platform device attributed to a session).
-- - `runner.billing_meter`: per VM, the time through which its runtime was measured; written in the transaction of the
--   `runtime` items of that interval, so a crash neither measures an interval twice nor loses one.
-- - `runner.credit_pause`: the sessions this device suspended because their organization's credit was used up;
--   `interrupted` = a turn was running (it gets the continuation prompt once the credit is back), `resumed_at` = when
--   the continuation was handed over (never twice).
ALTER TABLE runner.outbox DROP CONSTRAINT outbox_item_kind_check;
ALTER TABLE runner.outbox ADD CONSTRAINT outbox_item_kind_check CHECK (
    item_kind IN (
        'event', 'session_transition', 'vm_state', 'operation_transition', 'usage', 'quota', 'metrics',
        'audit', 'wrap', 'runtime'
    )
);

CREATE TABLE runner.billing_meter (
    vm_id text PRIMARY KEY,
    measured_through timestamptz NOT NULL
);

-- `runner.operator_origin`: the sessions that were ever bound to an operator subscription (ADR-052). The binding is
-- pruned when the session is no longer live; usage records processed later (journal lag, a restart backlog) must
-- still be charged as `operator`, so the origin is kept here.
CREATE TABLE runner.operator_origin (
    session_id uuid PRIMARY KEY,
    bound_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runner.credit_pause (
    session_id uuid PRIMARY KEY,
    org_id uuid NOT NULL,
    interrupted boolean NOT NULL,
    paused_at timestamptz NOT NULL,
    resumed_at timestamptz
);
