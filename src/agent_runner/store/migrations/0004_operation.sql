-- Operation pipeline (RUN-07, decisions/RUN-07.md): register-then-execute (05-runner.md section 12, 03-api.md
-- sections 8.3 and 9, ADR-012).
-- `operation` gains what the pipeline needs to resume after a restart and to answer the session: the guest's pending
-- call (`pending_id`, VM), the request as journaled (`requested_at`, `idempotency_key`, the guest's binding local
-- rejection), the output of the formal checks and pre-checks (`checked`), the registration body exactly as first sent
-- (`registration`, so a retry repeats the same bytes and the hub answers `replayed`), the hub's decision
-- (`duplicate_of`, `child_session_id`), the result event, and the answer given to the session.
ALTER TABLE runner.operation
    ADD COLUMN org_id uuid,
    ADD COLUMN vm_id text,
    ADD COLUMN pending_id uuid,
    ADD COLUMN requested_at text,
    ADD COLUMN idempotency_key text,
    ADD COLUMN local_error jsonb,
    ADD COLUMN checked jsonb,
    ADD COLUMN registration jsonb,
    ADD COLUMN duplicate_of uuid,
    ADD COLUMN child_session_id uuid,
    ADD COLUMN result_event_id uuid,
    ADD COLUMN retryable boolean,
    ADD COLUMN answer jsonb,
    ADD COLUMN answered_at timestamptz;

ALTER TABLE runner.operation
    ADD CONSTRAINT operation_state_check CHECK (
        state IN (
            'received', 'rejected', 'accepted', 'duplicate', 'denied', 'executing', 'succeeded', 'failed', 'expired'
        )
    );

CREATE INDEX operation_open ON runner.operation (state) WHERE state IN ('received', 'accepted', 'executing');
CREATE INDEX operation_task ON runner.operation (task_id, created_at);

-- Latest verified `settings.workgroup` event per workgroup ("latest wins" by the hub's scope order), kept as received
-- with the author's verified signing key; opened only into memory for the formal checks.
CREATE TABLE runner.workgroup_settings (
    wg_id uuid PRIMARY KEY,
    org_id uuid NOT NULL,
    event_id uuid NOT NULL,
    scope_seq bigint NOT NULL CHECK (scope_seq > 0),
    author_sig text NOT NULL,
    sealed jsonb NOT NULL
);

-- Verified `operation.result` events of the device's workgroups (any device), by operation: the result of an original
-- operation a duplicate returns (03-api.md section 9, result delivery).
CREATE TABLE runner.operation_result (
    operation_id uuid PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    author_sig text NOT NULL,
    sealed jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
