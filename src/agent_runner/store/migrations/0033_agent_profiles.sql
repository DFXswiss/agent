-- Rights profiles of the device's sessions (KOR-51, ADR-090 section 3; `profiles/state.py`).
--
-- `profile_state` keeps the last section `agent_profiles` of the desired state (one row): the effective profile of
-- every session the hub placed on the device and what each profile name resolves to per assigned workgroup. A
-- restarted daemon that cannot reach the hub therefore never forgets a restriction.
-- `session_profile` keeps the effective profile the hub answered for a child it accepted with a profile of its own
-- (`child_profile` of the registration), until the desired state lists the child; `profile` NULL = every right.
CREATE TABLE runner.profile_state (
    id smallint PRIMARY KEY CHECK (id = 1),
    section jsonb NOT NULL,
    received_at timestamptz NOT NULL
);
CREATE TABLE runner.session_profile (
    session_id uuid PRIMARY KEY,
    profile jsonb,
    created_at timestamptz NOT NULL
);
