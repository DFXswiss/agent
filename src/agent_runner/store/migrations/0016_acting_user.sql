-- Whose personal connection an operation uses (HOT-14, ADR-068): a personal connection must never be used on behalf
-- of another person. `prompt_event.author_id` is the verified author of a prompt (the inbox checked its signature and
-- `wg:write`); `session.turn_actor` the author of the prompts of the session's current turn (NULL: unknown, or prompts
-- of more than one person in one turn) and `session.turn_open` whether the turn is still running (no `idle` since its
-- first accepted prompt); `operation.actor_id` the acting user stamped at the receipt of the request. Rows written
-- before this migration have no author and no actor: they never use a personal connection.
ALTER TABLE runner.prompt_event ADD COLUMN author_id uuid;

ALTER TABLE runner.session
    ADD COLUMN turn_actor uuid,
    ADD COLUMN turn_open boolean NOT NULL DEFAULT false;

ALTER TABLE runner.operation ADD COLUMN actor_id uuid;

-- a child session's acting user is the one of the `subagent.request` that started it
CREATE INDEX operation_child ON runner.operation (child_session_id) WHERE child_session_id IS NOT NULL;
