-- Whose personal connection an operation uses (HOT-14, ADR-070): a personal connection must never be used on behalf
-- of another person. `prompt_event.author_id` is the verified author of a prompt (the inbox checked its signature and
-- `wg:write`); `session.turn_actor` the author of the prompts of the session's current turn (NULL: unknown, or prompts
-- of more than one person in one turn) and `session.turn_open` whether the turn is still running (no `idle` since its
-- first accepted prompt); `operation.actor_id` the acting user stamped at the receipt of the request. Operations
-- received before this migration have no acting user: they never use a personal connection.
ALTER TABLE runner.prompt_event ADD COLUMN author_id uuid;

-- prompts stored before: the inbox verified them with the author's key (`author_sig`), and the signature covers the
-- header, so the header's author is the verified author
UPDATE runner.prompt_event SET author_id = (sealed->'header'->>'author')::uuid
    WHERE sealed->'header'->>'author' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

ALTER TABLE runner.session
    ADD COLUMN turn_actor uuid,
    ADD COLUMN turn_open boolean NOT NULL DEFAULT false;

ALTER TABLE runner.operation ADD COLUMN actor_id uuid;

-- a child session's acting user is the one of the `subagent.request` that started it
CREATE INDEX operation_child ON runner.operation (child_session_id) WHERE child_session_id IS NOT NULL;
