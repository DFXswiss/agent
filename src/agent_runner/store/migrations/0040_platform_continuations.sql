-- Turns the platform continues by itself (FIX-02).
--
-- - `runner.platform_prompt`: a prompt the platform hands a session without a person writing it (the continuation
--   after credit, after a model pause, after a model rate limit). `actor_id` is the acting user of the turn it
--   continues, taken from `runner.session.turn_actor` when the prompt is registered (NULL: that turn had none). When
--   the guest accepts the prompt, the turn keeps that person (`credentials.acting.prompt_accepted`), so the work a
--   person asked for keeps using that person's connections after the platform continued it.
-- - `runner.turn_retry`: the main sessions whose last turn Claude Code ended with a retryable model error
--   (`StopFailure` `rate_limit`, `overloaded`, `server_error`): `due_at` is when the platform continues the turn,
--   `attempts` the continuations handed over since the last turn that ended normally, `continued_at` when the last
--   one was handed over (NULL: still due).
CREATE TABLE runner.platform_prompt (
    prompt_id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    actor_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runner.turn_retry (
    session_id uuid PRIMARY KEY,
    stop_error text NOT NULL CHECK (stop_error IN ('rate_limit', 'overloaded', 'server_error')),
    failed_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    due_at timestamptz NOT NULL,
    continued_at timestamptz
);
