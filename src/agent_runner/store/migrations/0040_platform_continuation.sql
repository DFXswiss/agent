-- A continuation the platform writes itself keeps the acting user of the turn it continues (HOT-28; ADR-070, ADR-081
-- section 2, ADR-083). `credit_pause.actor_id` and `model_pause.actor_id`: the acting user of the session's turn when
-- the pause was recorded (`session.turn_actor`; NULL: that turn had none). `platform_prompt`: the continuation prompts
-- the runner registered with a guest, with the person of the interrupted turn they continue (`actor_id`, NULL: none),
-- the cause (`credit` or `model`) and `accepted_at` once the guest accepted it (an acceptance counts once).
ALTER TABLE runner.credit_pause ADD COLUMN actor_id uuid;
ALTER TABLE runner.model_pause ADD COLUMN actor_id uuid;

CREATE TABLE runner.platform_prompt (
    prompt_id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    actor_id uuid,
    cause text NOT NULL CHECK (cause IN ('credit', 'model')),
    created_at timestamptz NOT NULL DEFAULT now(),
    accepted_at timestamptz
);
