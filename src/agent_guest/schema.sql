-- Guest journal schema, version 1 (docs/platform/spec/02-data-model.md section 5). Applied by agent-guest-control
-- at start when PRAGMA user_version is 0; the golden image version pins the schema (no migrations in place).
-- Additions to the specified tables (decisions/GST-01.md): journal.acked_at (ack time for the 24 h pruning),
-- pending.answered_at, and guest_meta (assignment and broken state that survive a restart of the control process).
-- prompt_registry.prompt_id (decisions/GST-03.md): the hook reports prompt_accepted {prompt_id} (03-api.md section 7.2).
-- ping.level, ping.ref and ping.noticed_at (decisions/RUN-14.md): get_notifications returns level and ref, and the
-- hook notice is attached once per batch (03-api.md sections 7.1, 8.1 and 8.2).
-- journal kinds platform.read and platform.refused (decisions/KOR-07.md, contracts/changes/KOR-07.md): the read get_notifications and MCP requests
-- refused before forwarding are journaled too (03-api.md section 8).
-- journal kind workvm.call and pending kind workvm (contracts/changes/KOR-29.md, 13-work-vm.md section 3): the calls of
-- the work VM tools, answered by the runner like a platform.query.
-- prompt_registry.speaker (ADR-083 section 1, contracts/changes/KOR-46.md): the sender line the runner registered with
-- the prompt; the hook decision returns it for exactly the row it accepted.
-- journal kind hub.relay and pending kind relay (contracts/changes/KOR-57.md, 05-runner.md section 17.2): the signed hub
-- requests of the orchestrator's agent tool, forwarded by the runner and answered with the hub's answer.

CREATE TABLE journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('session.state', 'session.output', 'session.tool', 'session.hook',
        'session.file', 'session.terminal', 'session.tools_ready', 'usage', 'operation.request', 'platform.query', 'workvm.call', 'hub.relay',
        'ping.delivered', 'platform.read', 'platform.refused', 'metrics', 'guest.error')),
    body TEXT NOT NULL,
    acked_at REAL
);
CREATE INDEX journal_acked ON journal (acked_at) WHERE acked_at IS NOT NULL;

CREATE TABLE session (
    id TEXT PRIMARY KEY,
    uid INTEGER NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    workspace TEXT NOT NULL,
    claude_session_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE prompt_registry (
    session_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    prompt_id TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    used_at TEXT,
    speaker TEXT,
    PRIMARY KEY (session_id, prompt_id)
);
CREATE INDEX prompt_registry_hash ON prompt_registry (session_id, sha256, used_at);

CREATE TABLE pending (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('operation', 'query', 'workvm', 'relay')),
    request TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('waiting', 'answered')),
    answer TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE INDEX pending_session ON pending (session_id, state);

CREATE TABLE ping (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('whiteboard.entry', 'subagent.finished', 'operation.finished')),
    level TEXT,
    ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    noticed_at TEXT,
    delivered_at TEXT
);
CREATE INDEX ping_open ON ping (session_id, delivered_at);

CREATE TABLE guest_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 1;
