-- Latest profile events of the device's scopes (RUN-04, decisions/RUN-04.md): the names the harness context of a
-- session needs (05-runner.md section 8). Each row keeps the event as received (sealed: ciphertext and signature),
-- the author's verified signing key and the signed header fields that decide "latest wins"; the plaintext is opened
-- only into memory with the scope key when a session starts.
CREATE TABLE runner.profile_event (
    kind text NOT NULL CHECK (kind IN ('org.profile', 'wg.profile', 'member.profile', 'task.meta')),
    org_id uuid NOT NULL,
    object_id uuid NOT NULL,
    event_id uuid NOT NULL,
    ts text NOT NULL,
    author_sig text NOT NULL,
    sealed jsonb NOT NULL,
    PRIMARY KEY (kind, org_id, object_id)
);
