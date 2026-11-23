-- Prompt delivery (RUN-05, decisions/RUN-05.md): the verified prompt events of the device's workgroups and the
-- routing of each prompt to the session it is delivered to (05-runner.md sections 2.2 step 6, 6.2, 6.3).
-- `prompt_event` keeps each event exactly as received (ciphertext and signature) with the author's verified signing
-- key; the text is opened only into memory when the prompt is delivered. `prompt_delivery` gains the task, the
-- event's position in its workgroup scope (delivery order) and the channel the prompt was handed over by.
CREATE TABLE runner.prompt_event (
    event_id uuid PRIMARY KEY,
    org_id uuid NOT NULL,
    wg_id uuid NOT NULL,
    task_id uuid NOT NULL,
    scope_seq bigint NOT NULL CHECK (scope_seq > 0),
    author_sig text NOT NULL,
    sealed jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (wg_id, scope_seq)
);

CREATE INDEX prompt_event_task ON runner.prompt_event (task_id, scope_seq);

ALTER TABLE runner.prompt_delivery
    ADD COLUMN task_id uuid,
    ADD COLUMN scope_seq bigint,
    ADD COLUMN channel text NOT NULL DEFAULT 'prompt' CHECK (channel IN ('start', 'prompt')),
    ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0);

CREATE INDEX prompt_delivery_session ON runner.prompt_delivery (session_id, scope_seq);
