-- Platform reads of the sessions (RUN-08, decisions/RUN-08.md): the verified events the MCP read tools answer from
-- (03-api.md section 8.1, ADR-024): prompts and session outputs (`get_task_messages`), whiteboard entries, edits and
-- deletes on every level (`get_whiteboard`) and device profiles (device names in `list_members`). Each row keeps the
-- event exactly as received (ciphertext and signature) with the author's verified signing key and the clear header
-- fields the reads select by; the text is opened only into memory with the scope key when a session reads it.
-- `scope_seq` is the hub's order of the scope, `ts` the signed author time, `received_at` the hub's receive time.
CREATE TABLE runner.read_event (
    event_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (
        kind IN ('prompt', 'session.output', 'whiteboard.entry', 'whiteboard.edit', 'whiteboard.delete', 'device.profile')
    ),
    org_id uuid NOT NULL,
    scope text NOT NULL CHECK (scope IN ('org', 'wg')),
    scope_id uuid NOT NULL,
    scope_seq bigint NOT NULL CHECK (scope_seq > 0),
    task_id uuid,
    ref uuid,
    author_id uuid NOT NULL,
    ts text NOT NULL,
    received_at text NOT NULL,
    author_sig text NOT NULL,
    sealed jsonb NOT NULL,
    UNIQUE (scope_id, scope_seq)
);

CREATE INDEX read_event_messages ON runner.read_event (scope_id, task_id, scope_seq)
    WHERE kind IN ('prompt', 'session.output');
CREATE INDEX read_event_boards ON runner.read_event (scope_id, scope_seq)
    WHERE kind IN ('whiteboard.entry', 'whiteboard.edit', 'whiteboard.delete');
CREATE INDEX read_event_devices ON runner.read_event (org_id, ref) WHERE kind = 'device.profile';
