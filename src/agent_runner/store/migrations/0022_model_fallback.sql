-- Model access fallback (KOR-50, ADR-080 sections 2 to 4).
--
-- - `runner.access_limit`: the subscription accesses this device found used up or refused, kept across restarts.
--   `access_key` names one access (`own:<owner_id>`, `operator:<file name>`, `dfxkey:<device_id>`), never a token.
--   `exhausted` rows carry the release time `until` (the provider's own when `readable`, else a probe time after a
--   streak of `429` answers); a row whose `until` passed is released. `blocked` rows are the ADR-052 `401`/`403` block
--   of an operator token file with the file's identity (device, inode, modification time, size): a new file content
--   releases it.
-- - `runner.model_pause`: the main sessions this device paused because their model access was used up (`wait`: no
--   allowed access, until `until`; `dfx_choice` = DFX access would be available but the owner's setting bars it) or
--   moved to another kind of access (`switch`: paused and resumed at once). `interrupted` = a turn was running (it gets
--   the continuation sentence), `suspended` = the guest suspended it, `resumed_at` = the pause ended (never twice).
CREATE TABLE runner.access_limit (
    access_key text PRIMARY KEY CHECK (
        access_key ~ '^(own:[0-9a-f-]{36}|operator:[A-Za-z0-9][A-Za-z0-9._-]{0,63}|dfxkey:[0-9a-f-]{36})$'
    ),
    kind text NOT NULL CHECK (kind IN ('exhausted', 'blocked')),
    until timestamptz,
    readable boolean NOT NULL,
    limited_at timestamptz NOT NULL,
    identity text CHECK (identity ~ '^[0-9]+:[0-9]+:[0-9]+:[0-9]+$'),
    CHECK ((kind = 'exhausted') = (until IS NOT NULL)),
    CHECK ((kind = 'blocked') = (identity IS NOT NULL))
);

CREATE TABLE runner.model_pause (
    session_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('switch', 'wait')),
    interrupted boolean NOT NULL,
    paused_at timestamptz NOT NULL,
    until timestamptz,
    dfx_choice boolean NOT NULL DEFAULT false,
    from_access text NOT NULL CHECK (from_access IN ('subscription', 'api_key', 'operator', 'platform')),
    from_model text,
    suspended boolean NOT NULL DEFAULT false,
    resumed_at timestamptz
);
