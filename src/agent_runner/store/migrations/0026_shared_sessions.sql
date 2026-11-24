-- Shared sessions (KOR-46, ADR-083 sections 1-3).
--
-- `prompt_delivery.via_agent`: the hub reports that it received the prompt from its author's agent (the `prompt`
-- hint's `via_agent`, `prompt_queue.via_agent` of the device state); it only adds the qualifier "through their agent"
-- to the sender line and stays true once set. `withdrawn` is a new final delivery state: the author took the prompt
-- back while its row was still `pending` (never handed to the guest, never delivered again).
ALTER TABLE runner.prompt_delivery ADD COLUMN via_agent boolean NOT NULL DEFAULT false;
ALTER TABLE runner.prompt_delivery DROP CONSTRAINT prompt_delivery_state_check;
ALTER TABLE runner.prompt_delivery ADD CONSTRAINT prompt_delivery_state_check
    CHECK (state IN ('pending', 'registered', 'typed', 'accepted', 'refused', 'failed', 'withdrawn'));

-- `prompt_withdrawal`: every withdrawal this device carried out and reported (outbox audit `session.prompt_withdrawn`,
-- once per event). A prompt withdrawn before the hub's routing reached this device has no delivery row yet; its
-- routing then records the row `withdrawn` at once.
CREATE TABLE runner.prompt_withdrawal (
    event_id uuid PRIMARY KEY,
    withdrawn_at timestamptz NOT NULL DEFAULT now()
);

-- `workvm.triggered_by`: the person whose turn requested the work VM (the operation's acting user, ADR-070), reported
-- with its runtime (`triggered_by` of the outbox `runtime` items, ADR-083 section 2); NULL when unknown.
ALTER TABLE runner.workvm ADD COLUMN triggered_by uuid;
