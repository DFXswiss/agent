-- 0001_hub: the complete hub schema of docs/platform/spec/02-data-model.md sections 1 and 2.
--
-- Applied by `agent-hub migrate` (agent_hub.db.migrate) as the owner role, in one transaction. The file is
-- composed by the driver before it runs: `{database}` and `{writer}` are the database and the writer role as
-- SQL identifiers; every literal brace is doubled. Statements name `hub.` in full (the run's search_path is
-- `pg_catalog, pg_temp`). Columns without NULL in section 2 are NOT NULL; the exceptions are recorded in
-- docs/platform/decisions/HUB-01.md. [AO] tables carry the hub.refuse_change() triggers; the writer gets
-- SELECT, INSERT on them and SELECT, INSERT, UPDATE, DELETE on every other table (section 1.4).

REVOKE ALL ON DATABASE {database} FROM PUBLIC;
GRANT CONNECT ON DATABASE {database} TO {writer};

CREATE SCHEMA hub;
REVOKE ALL ON SCHEMA hub FROM PUBLIC;
GRANT USAGE ON SCHEMA hub TO {writer};

CREATE TABLE hub.schema_version (
  version    integer     PRIMARY KEY CHECK (version >= 1),
  name       text        NOT NULL CHECK (name ~ '^[a-z0-9_]{{1,64}}$'),
  checksum   text        NOT NULL CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
  applied_at timestamptz NOT NULL
);

CREATE FUNCTION hub.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only rows are never changed or removed' USING ERRCODE = 'P0001';
END
$$;
REVOKE ALL ON FUNCTION hub.refuse_change() FROM PUBLIC;

-- 2.1 Principals, authentication -------------------------------------------------------------------------

CREATE TABLE hub.principal (
  id          uuid        PRIMARY KEY,
  kind        text        NOT NULL CHECK (kind IN ('user', 'device')),
  created_at  timestamptz NOT NULL,
  disabled_at timestamptz NULL
);

CREATE TABLE hub.user_identity (
  user_id     uuid        PRIMARY KEY REFERENCES hub.principal (id),
  kem_pub     bytea       NOT NULL CHECK (octet_length(kem_pub) = 32),
  sig_pub     bytea       NOT NULL CHECK (octet_length(sig_pub) = 32),
  recovery_id text        NOT NULL UNIQUE,
  created_at  timestamptz NOT NULL
);

CREATE TABLE hub.user_settings (
  user_id      uuid        PRIMARY KEY REFERENCES hub.principal (id),
  model_access text        NOT NULL DEFAULT 'auto'
               CHECK (model_access IN ('auto', 'subscription', 'api_key', 'platform')),
  model_id     text        NULL,
  locale       text        NOT NULL DEFAULT 'en' CHECK (locale IN ('de', 'en')),
  updated_at   timestamptz NOT NULL
);

CREATE TABLE hub.identity_bundle (
  user_id    uuid        NOT NULL,
  slot       text        NOT NULL CHECK (slot = 'recovery' OR slot ~ '^passkey:[A-Za-z0-9_-]+$'),
  ct         bytea       NOT NULL,
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (user_id, slot)
);

CREATE TABLE hub.passkey (
  credential_id bytea       PRIMARY KEY CHECK (octet_length(credential_id) BETWEEN 1 AND 1023),
  user_id       uuid        NOT NULL,
  public_key    bytea       NOT NULL CHECK (octet_length(public_key) = 65 AND get_byte(public_key, 0) = 4),
  sign_count    bigint      NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
  transports    text[]      NOT NULL DEFAULT '{{}}',
  prf_seen      boolean     NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL,
  last_used_at  timestamptz NOT NULL,
  removed_at    timestamptz NULL
);
CREATE INDEX passkey_user ON hub.passkey (user_id);

CREATE TABLE hub.ceremony (
  id         uuid        PRIMARY KEY,
  kind       text        NOT NULL CHECK (kind IN ('register', 'login', 'add_passkey', 'recovery')),
  challenge  bytea       NOT NULL CHECK (octet_length(challenge) = 32),
  user_id    uuid        NULL,
  invite_id  text        NULL,
  hub_value  bytea       NULL,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL CHECK (expires_at > created_at),
  used_at    timestamptz NULL
);
CREATE INDEX ceremony_created ON hub.ceremony (created_at);

CREATE TABLE hub.browser_session (
  key_id        text        PRIMARY KEY CHECK (key_id ~ '^session\.[0-9a-f]{{32}}$'),
  user_id       uuid        NOT NULL,
  credential_id bytea       NOT NULL,
  public_key    bytea       NOT NULL CHECK (octet_length(public_key) = 65 AND get_byte(public_key, 0) = 4),
  statement     text        NOT NULL,
  created_at    timestamptz NOT NULL,
  expires_at    timestamptz NOT NULL CHECK (expires_at > created_at),
  ended_at      timestamptz NULL
);
CREATE INDEX browser_session_user ON hub.browser_session (user_id);

CREATE TABLE hub.request_nonce (
  key_id  text        NOT NULL,
  nonce   text        NOT NULL,
  seen_at timestamptz NOT NULL,
  PRIMARY KEY (key_id, nonce)
);
CREATE INDEX request_nonce_seen ON hub.request_nonce (seen_at);

CREATE TABLE hub.device (
  id            uuid        PRIMARY KEY REFERENCES hub.principal (id),
  kind          text        NOT NULL CHECK (kind IN ('platform', 'org')),
  kem_pub       bytea       NOT NULL CHECK (octet_length(kem_pub) = 32),
  sig_pub       bytea       NOT NULL CHECK (octet_length(sig_pub) = 32),
  owner_org     uuid        NULL,
  state         text        NOT NULL
                CHECK (state IN ('pending_certification', 'active', 'decertified', 'removed')),
  enrolled_at   timestamptz NOT NULL,
  certified_seq integer     NULL CHECK (certified_seq >= 0),
  agent_version text        NOT NULL,
  os            text        NOT NULL,
  arch          text        NOT NULL,
  capacity      jsonb       NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(capacity) = 'object'),
  updated_at    timestamptz NOT NULL,
  CHECK ((kind = 'org') = (owner_org IS NOT NULL))
);
CREATE INDEX device_owner_org ON hub.device (owner_org) WHERE owner_org IS NOT NULL;

CREATE TABLE hub.device_presence (
  device_id         uuid        PRIMARY KEY,
  hub_instance      text        NOT NULL,
  connected_at      timestamptz NOT NULL,
  last_heartbeat_at timestamptz NOT NULL
);

CREATE TABLE hub.device_enrollment (
  invite_id    text        PRIMARY KEY,
  device_id    uuid        NOT NULL,
  request      jsonb       NOT NULL CHECK (jsonb_typeof(request) = 'object'),
  created_at   timestamptz NOT NULL,
  certified_at timestamptz NULL
);

CREATE TABLE hub.invite (
  id              text        PRIMARY KEY,
  scope           text        NOT NULL CHECK (scope IN ('platform', 'org')),
  kind            text        NOT NULL CHECK (kind IN ('user', 'device')),
  org_id          uuid        NULL,
  log_seq         integer     NULL CHECK (log_seq >= 0),
  created_by      uuid        NULL,
  created_at      timestamptz NOT NULL,
  expires_at      timestamptz NOT NULL CHECK (expires_at > created_at),
  registered_user uuid        NULL UNIQUE,
  registered_at   timestamptz NULL,
  used_at         timestamptz NULL,
  used_by         uuid        NULL,
  revoked_at      timestamptz NULL,
  package_ct      bytea       NULL,
  CHECK (scope = 'platform' OR org_id IS NOT NULL),
  CHECK ((registered_user IS NULL) = (registered_at IS NULL))
);
CREATE INDEX invite_org ON hub.invite (org_id, created_at DESC, id DESC) WHERE org_id IS NOT NULL;

-- 2.2 Organizations, logs, keys --------------------------------------------------------------------------

CREATE TABLE hub.org (
  id               uuid        PRIMARY KEY,
  private          boolean     NOT NULL,
  platform_access  boolean     NOT NULL DEFAULT false,
  default_wg       uuid        NOT NULL UNIQUE,
  created_by       uuid        NOT NULL,
  created_at       timestamptz NOT NULL,
  log_head_seq     integer     NOT NULL CHECK (log_head_seq >= 0),
  log_head_hash    text        NOT NULL,
  rotation_pending jsonb       NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(rotation_pending) = 'array')
);

CREATE TABLE hub.membership_entry (
  org_id      uuid        NOT NULL,
  seq         integer     NOT NULL CHECK (seq >= 0),
  entry_text  text        NOT NULL,
  entry_hash  text        NOT NULL UNIQUE,
  type        text        NOT NULL,
  by_id       uuid        NOT NULL,
  subject_id  uuid        NULL,
  received_at timestamptz NOT NULL,
  PRIMARY KEY (org_id, seq)
);
CREATE INDEX membership_entry_subject ON hub.membership_entry (subject_id) WHERE subject_id IS NOT NULL;

CREATE TABLE hub.platform_entry (
  seq         integer     PRIMARY KEY CHECK (seq >= 0),
  entry_text  text        NOT NULL,
  entry_hash  text        NOT NULL UNIQUE,
  type        text        NOT NULL,
  subject_id  uuid        NULL,
  received_at timestamptz NOT NULL
);

CREATE TABLE hub.org_member (
  org_id       uuid    NOT NULL,
  principal_id uuid    NOT NULL,
  kind         text    NOT NULL CHECK (kind IN ('user', 'device')),
  role         text    NOT NULL CHECK (role IN ('admin', 'member', 'device')),
  joined_seq   integer NOT NULL CHECK (joined_seq >= 0),
  removed_seq  integer NULL,
  PRIMARY KEY (org_id, principal_id),
  CHECK ((kind = 'device') = (role = 'device')),
  CHECK (removed_seq IS NULL OR removed_seq > joined_seq)
);
CREATE INDEX org_member_principal ON hub.org_member (principal_id);

CREATE TABLE hub.wg (
  id           uuid    PRIMARY KEY,
  org_id       uuid    NOT NULL,
  is_default   boolean NOT NULL,
  created_seq  integer NOT NULL CHECK (created_seq >= 0),
  archived_seq integer NULL,
  CHECK (NOT (is_default AND archived_seq IS NOT NULL))
);
CREATE INDEX wg_org ON hub.wg (org_id);
CREATE UNIQUE INDEX wg_one_default ON hub.wg (org_id) WHERE is_default;

CREATE TABLE hub.wg_access (
  wg_id       uuid    NOT NULL,
  user_id     uuid    NOT NULL,
  access      text    NOT NULL CHECK (access IN ('read', 'write')),
  granted_seq integer NOT NULL CHECK (granted_seq >= 0),
  revoked_seq integer NULL,
  PRIMARY KEY (wg_id, user_id)
);
CREATE INDEX wg_access_user ON hub.wg_access (user_id);

CREATE TABLE hub.wg_device (
  wg_id          uuid    NOT NULL,
  device_id      uuid    NOT NULL,
  assigned_seq   integer NOT NULL CHECK (assigned_seq >= 0),
  unassigned_seq integer NULL,
  PRIMARY KEY (wg_id, device_id)
);
CREATE INDEX wg_device_device ON hub.wg_device (device_id);

CREATE TABLE hub.platform_role (
  user_id     uuid    NOT NULL,
  role        text    NOT NULL CHECK (role IN ('inviter')),
  granted_seq integer NOT NULL CHECK (granted_seq >= 0),
  revoked_seq integer NULL,
  PRIMARY KEY (user_id, role)
);

CREATE TABLE hub.scope_epoch (
  scope       text        NOT NULL CHECK (scope IN ('org', 'wg', 'cred')),
  scope_id    uuid        NOT NULL,
  epoch       integer     NOT NULL CHECK (epoch >= 1),
  created_at  timestamptz NOT NULL,
  created_seq integer     NULL,
  PRIMARY KEY (scope, scope_id, epoch)
);

CREATE TABLE hub.key_wrap (
  scope        text        NOT NULL CHECK (scope IN ('org', 'wg', 'cred')),
  scope_id     uuid        NOT NULL,
  epoch        integer     NOT NULL CHECK (epoch >= 1),
  recipient_id uuid        NOT NULL,
  wrap         bytea       NOT NULL,
  wrapped_by   uuid        NOT NULL,
  sig          bytea       NOT NULL CHECK (octet_length(sig) = 64),
  created_at   timestamptz NOT NULL,
  PRIMARY KEY (scope, scope_id, epoch, recipient_id)
);
CREATE INDEX key_wrap_recipient ON hub.key_wrap (recipient_id);

-- 2.3 Events ---------------------------------------------------------------------------------------------

CREATE TABLE hub.scope_head (
  scope_id uuid   PRIMARY KEY,
  scope    text   NOT NULL CHECK (scope IN ('org', 'wg')),
  seq      bigint NOT NULL CHECK (seq >= 0)
);

CREATE TABLE hub.event (
  scope_id         uuid        NOT NULL,
  scope_seq        bigint      NOT NULL CHECK (scope_seq >= 1),
  id               uuid        NOT NULL UNIQUE,
  scope            text        NOT NULL CHECK (scope IN ('org', 'wg')),
  org_id           uuid        NOT NULL,
  wg_id            uuid        NULL,
  task_id          uuid        NULL,
  session_id       uuid        NULL,
  author_id        uuid        NOT NULL,
  author_kind      text        NOT NULL CHECK (author_kind IN ('user', 'device')),
  kind             text        NOT NULL,
  ref              text        NOT NULL,
  epoch            integer     NOT NULL CHECK (epoch >= 1),
  ts               timestamptz NOT NULL,
  lh               integer     NOT NULL CHECK (lh >= 0),
  header_text      text        NOT NULL,
  ct               bytea       NOT NULL,
  sig              bytea       NOT NULL CHECK (octet_length(sig) = 64),
  size             integer     NOT NULL CHECK (size >= 0),
  origin_device_id uuid        NULL,
  origin_seq       bigint      NULL CHECK (origin_seq >= 1),
  received_at      timestamptz NOT NULL,
  PRIMARY KEY (scope_id, scope_seq),
  CHECK ((origin_device_id IS NULL) = (origin_seq IS NULL))
);
CREATE INDEX event_task ON hub.event (task_id, scope_seq);
CREATE INDEX event_session_kind ON hub.event (session_id, kind, scope_seq);
CREATE INDEX event_kind_ref ON hub.event (kind, ref);
CREATE INDEX event_received ON hub.event (received_at);

CREATE TABLE hub.device_stream (
  device_id       uuid   PRIMARY KEY,
  last_origin_seq bigint NOT NULL CHECK (last_origin_seq >= 0)
);

-- 2.4 Tasks, sessions, VMs -------------------------------------------------------------------------------

CREATE TABLE hub.task (
  id               uuid        PRIMARY KEY,
  org_id           uuid        NOT NULL,
  wg_id            uuid        NOT NULL,
  owner_id         uuid        NOT NULL,
  state            text        NOT NULL
                   CHECK (state IN ('scheduling', 'starting', 'working', 'idle', 'failed', 'closed')),
  state_reason     text        NULL,
  created_at       timestamptz NOT NULL,
  state_at         timestamptz NOT NULL,
  last_activity_at timestamptz NOT NULL,
  closed_at        timestamptz NULL,
  CHECK ((state = 'closed') = (closed_at IS NOT NULL))
);
CREATE INDEX task_wg ON hub.task (wg_id, created_at DESC, id DESC);
CREATE INDEX task_owner ON hub.task (owner_id, created_at DESC, id DESC);

CREATE TABLE hub.task_transition (
  task_id uuid        NOT NULL,
  seq     integer     NOT NULL CHECK (seq >= 1),
  state   text        NOT NULL
          CHECK (state IN ('scheduling', 'starting', 'working', 'idle', 'failed', 'closed')),
  reason  text        NULL,
  at      timestamptz NOT NULL,
  by_id   uuid        NULL,
  PRIMARY KEY (task_id, seq)
);

CREATE TABLE hub.session (
  id                  uuid        PRIMARY KEY,
  task_id             uuid        NOT NULL,
  org_id              uuid        NOT NULL,
  wg_id               uuid        NOT NULL,
  owner_id            uuid        NOT NULL,
  device_id           uuid        NULL,
  vm_id               text        NULL CHECK (vm_id ~ '^vm-[0-9a-f]{{8}}$'),
  parent_session_id   uuid        NULL,
  origin_operation_id uuid        NULL,
  depth               integer     NOT NULL CHECK (depth >= 0),
  state               text        NOT NULL CHECK (state IN (
                        'requested', 'vm_assigning', 'vm_ready', 'sandbox_ready', 'agent_starting',
                        'prompt_delivering', 'working', 'idle', 'suspended', 'resuming', 'stopping', 'stopped',
                        'failed')),
  state_reason        text        NULL,
  model_access        text        NULL CHECK (model_access IN ('subscription', 'api_key', 'platform')),
  model_id            text        NULL,
  agent_kind          text        NOT NULL DEFAULT 'claude-code',
  agent_version       text        NULL,
  estimate_ms         integer     NULL CHECK (estimate_ms >= 0),
  created_at          timestamptz NOT NULL,
  ready_at            timestamptz NULL,
  first_working_at    timestamptz NULL,
  ended_at            timestamptz NULL,
  CHECK ((depth = 0) = (parent_session_id IS NULL))
);
CREATE INDEX session_task ON hub.session (task_id, created_at, id);
CREATE INDEX session_device ON hub.session (device_id) WHERE device_id IS NOT NULL;
CREATE INDEX session_parent ON hub.session (parent_session_id) WHERE parent_session_id IS NOT NULL;

CREATE TABLE hub.session_transition (
  session_id  uuid        NOT NULL,
  seq         integer     NOT NULL CHECK (seq >= 1),
  state       text        NOT NULL CHECK (state IN (
                'requested', 'vm_assigning', 'vm_ready', 'sandbox_ready', 'agent_starting', 'prompt_delivering',
                'working', 'idle', 'suspended', 'resuming', 'stopping', 'stopped', 'failed')),
  reason      text        NULL,
  at          timestamptz NOT NULL,
  received_at timestamptz NOT NULL,
  reported_by uuid        NULL,
  PRIMARY KEY (session_id, seq)
);

CREATE TABLE hub.vm (
  id             text        PRIMARY KEY CHECK (id ~ '^vm-[0-9a-f]{{8}}$'),
  device_id      uuid        NOT NULL,
  wg_id          uuid        NULL,
  state          text        NOT NULL
                 CHECK (state IN ('pool', 'assigning', 'running', 'stopping', 'stopped', 'deleted')),
  golden_version text        NOT NULL,
  cpus           integer     NOT NULL CHECK (cpus >= 1),
  memory_mib     integer     NOT NULL CHECK (memory_mib >= 1),
  disk_gib       integer     NOT NULL CHECK (disk_gib >= 1),
  created_at     timestamptz NOT NULL,
  state_at       timestamptz NOT NULL
);
CREATE INDEX vm_device ON hub.vm (device_id);
CREATE INDEX vm_wg ON hub.vm (wg_id) WHERE wg_id IS NOT NULL;

-- 2.5 Operations -----------------------------------------------------------------------------------------

CREATE TABLE hub.operation (
  id              uuid        PRIMARY KEY,
  org_id          uuid        NOT NULL,
  wg_id           uuid        NOT NULL,
  task_id         uuid        NOT NULL,
  session_id      uuid        NOT NULL,
  device_id       uuid        NOT NULL,
  actor_id        uuid        NOT NULL,
  type            text        NOT NULL,
  state           text        NOT NULL CHECK (state IN (
                    'received', 'rejected', 'accepted', 'duplicate', 'denied', 'executing', 'succeeded', 'failed',
                    'expired')),
  error_code      text        NULL,
  retryable       boolean     NOT NULL DEFAULT false,
  dedupe_epoch    integer     NULL,
  dedupe_tag      text        NULL,
  duplicate_of    uuid        NULL,
  attempt         integer     NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  params_event_id uuid        NULL,
  result_event_id uuid        NULL,
  created_at      timestamptz NOT NULL,
  decided_at      timestamptz NULL,
  started_at      timestamptz NULL,
  finished_at     timestamptz NULL,
  CHECK ((dedupe_epoch IS NULL) = (dedupe_tag IS NULL)),
  CHECK ((state = 'duplicate') = (duplicate_of IS NOT NULL))
);
CREATE INDEX operation_task ON hub.operation (task_id, created_at DESC, id DESC);
CREATE INDEX operation_session ON hub.operation (session_id, created_at DESC, id DESC);
CREATE INDEX operation_dedupe ON hub.operation (wg_id, dedupe_epoch, dedupe_tag) WHERE dedupe_tag IS NOT NULL;

CREATE TABLE hub.operation_transition (
  operation_id uuid        NOT NULL,
  seq          integer     NOT NULL CHECK (seq >= 1),
  state        text        NOT NULL CHECK (state IN (
                 'received', 'rejected', 'accepted', 'duplicate', 'denied', 'executing', 'succeeded', 'failed',
                 'expired')),
  error_code   text        NULL,
  at           timestamptz NOT NULL,
  by_id        uuid        NULL,
  PRIMARY KEY (operation_id, seq)
);

CREATE TABLE hub.dedupe_slot (
  wg_id        uuid        NOT NULL,
  epoch        integer     NOT NULL,
  tag          text        NOT NULL,
  operation_id uuid        NOT NULL UNIQUE,
  expires_at   timestamptz NOT NULL,
  PRIMARY KEY (wg_id, epoch, tag)
);
CREATE INDEX dedupe_slot_expires ON hub.dedupe_slot (expires_at);

-- 2.6 Whiteboards and notifications ----------------------------------------------------------------------

CREATE TABLE hub.whiteboard_entry (
  id               uuid        PRIMARY KEY,
  org_id           uuid        NOT NULL,
  level            text        NOT NULL CHECK (level IN ('org', 'workgroup', 'task')),
  wg_id            uuid        NULL,
  task_id          uuid        NULL,
  author_id        uuid        NOT NULL,
  created_event_id uuid        NOT NULL,
  latest_event_id  uuid        NOT NULL,
  created_at       timestamptz NOT NULL,
  updated_at       timestamptz NOT NULL,
  deleted          boolean     NOT NULL DEFAULT false,
  notify           boolean     NOT NULL DEFAULT false,
  CHECK ((level = 'org' AND wg_id IS NULL AND task_id IS NULL)
      OR (level = 'workgroup' AND wg_id IS NOT NULL AND task_id IS NULL)
      OR (level = 'task' AND wg_id IS NOT NULL AND task_id IS NOT NULL))
);
CREATE INDEX whiteboard_entry_scope ON hub.whiteboard_entry (org_id, level, wg_id, task_id, created_at DESC, id DESC);

CREATE TABLE hub.session_ping (
  id           uuid        PRIMARY KEY,
  session_id   uuid        NOT NULL,
  kind         text        NOT NULL CHECK (kind IN ('whiteboard.entry', 'subagent.finished', 'operation.finished')),
  level        text        NULL CHECK (level IN ('org', 'workgroup', 'task')),
  ref          text        NOT NULL,
  created_at   timestamptz NOT NULL,
  delivered_at timestamptz NULL
);
CREATE INDEX session_ping_undelivered ON hub.session_ping (session_id, created_at) WHERE delivered_at IS NULL;

CREATE TABLE hub.notification (
  id           uuid        PRIMARY KEY,
  recipient_id uuid        NOT NULL,
  kind         text        NOT NULL CHECK (kind IN (
                 'whiteboard.entry', 'task.idle', 'task.failed', 'invite.accepted', 'rotation.required')),
  org_id       uuid        NOT NULL,
  wg_id        uuid        NULL,
  task_id      uuid        NULL,
  ref          text        NULL,
  created_at   timestamptz NOT NULL,
  read_at      timestamptz NULL
);
CREATE INDEX notification_recipient ON hub.notification (recipient_id, created_at DESC, id DESC);

-- 2.7 Credentials and device requests --------------------------------------------------------------------

CREATE TABLE hub.credential (
  id          uuid        PRIMARY KEY,
  owner_id    uuid        NOT NULL,
  kind        text        NOT NULL,
  version     integer     NOT NULL CHECK (version >= 1),
  epoch       integer     NOT NULL CHECK (epoch >= 1),
  header_text text        NOT NULL,
  ct          bytea       NOT NULL,
  sig         bytea       NOT NULL CHECK (octet_length(sig) = 64),
  updated_at  timestamptz NOT NULL,
  UNIQUE (owner_id, kind)
);

CREATE TABLE hub.device_request (
  id         uuid        PRIMARY KEY,
  user_id    uuid        NOT NULL,
  device_id  uuid        NOT NULL,
  kind       text        NOT NULL
             CHECK (kind IN ('github.connect', 'claude.login', 'credential.check', 'mailbox.check')),
  state      text        NOT NULL CHECK (state IN ('pending', 'delivered', 'answered', 'failed', 'expired')),
  envelope   text        NOT NULL,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL CHECK (expires_at > created_at)
);
CREATE INDEX device_request_device ON hub.device_request (device_id, state);
CREATE INDEX device_request_user ON hub.device_request (user_id, created_at DESC, id DESC);

CREATE TABLE hub.device_response (
  request_id uuid        NOT NULL,
  seq        integer     NOT NULL CHECK (seq >= 1),
  envelope   text        NOT NULL,
  created_at timestamptz NOT NULL,
  PRIMARY KEY (request_id, seq)
);

-- 2.8 Metrics, usage, quotas -----------------------------------------------------------------------------

CREATE TABLE hub.metric_sample (
  entity_kind      text        NOT NULL CHECK (entity_kind IN ('device', 'vm', 'session')),
  entity_id        text        NOT NULL,
  ts               timestamptz NOT NULL,
  device_id        uuid        NOT NULL,
  cpu_ratio        real        NULL CHECK (cpu_ratio >= 0),
  mem_used_bytes   bigint      NULL CHECK (mem_used_bytes >= 0),
  mem_limit_bytes  bigint      NULL CHECK (mem_limit_bytes >= 0),
  disk_used_bytes  bigint      NULL CHECK (disk_used_bytes >= 0),
  disk_limit_bytes bigint      NULL CHECK (disk_limit_bytes >= 0),
  net_rx_bytes     bigint      NULL CHECK (net_rx_bytes >= 0),
  net_tx_bytes     bigint      NULL CHECK (net_tx_bytes >= 0),
  uptime_seconds   bigint      NULL CHECK (uptime_seconds >= 0),
  reachable        boolean     NOT NULL,
  state            text        NOT NULL,
  free_vm_slots    integer     NULL CHECK (free_vm_slots >= 0),
  free_mem_bytes   bigint      NULL CHECK (free_mem_bytes >= 0),
  PRIMARY KEY (entity_kind, entity_id, ts)
);
CREATE INDEX metric_sample_ts ON hub.metric_sample (ts);
CREATE INDEX metric_sample_device ON hub.metric_sample (device_id, ts);

CREATE TABLE hub.metric_rollup (
  entity_kind          text        NOT NULL CHECK (entity_kind IN ('device', 'vm', 'session')),
  entity_id            text        NOT NULL,
  ts                   timestamptz NOT NULL
                       CHECK (extract(epoch FROM ts) % 300 = 0),
  device_id            uuid        NOT NULL,
  samples              integer     NOT NULL CHECK (samples >= 1),
  cpu_ratio_avg        real        NULL,
  cpu_ratio_max        real        NULL,
  mem_used_bytes_avg   bigint      NULL,
  mem_used_bytes_max   bigint      NULL,
  mem_limit_bytes_avg  bigint      NULL,
  mem_limit_bytes_max  bigint      NULL,
  disk_used_bytes_avg  bigint      NULL,
  disk_used_bytes_max  bigint      NULL,
  disk_limit_bytes_avg bigint      NULL,
  disk_limit_bytes_max bigint      NULL,
  free_vm_slots_avg    real        NULL,
  free_vm_slots_max    integer     NULL,
  free_mem_bytes_avg   bigint      NULL,
  free_mem_bytes_max   bigint      NULL,
  net_rx_bytes         bigint      NULL,
  net_tx_bytes         bigint      NULL,
  uptime_seconds       bigint      NULL,
  reachable            boolean     NOT NULL,
  state                text        NOT NULL,
  PRIMARY KEY (entity_kind, entity_id, ts)
);
CREATE INDEX metric_rollup_ts ON hub.metric_rollup (ts);

CREATE TABLE hub.usage_counter (
  session_id            uuid        PRIMARY KEY,
  requests              bigint      NOT NULL DEFAULT 0 CHECK (requests >= 0),
  input_tokens          bigint      NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens         bigint      NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cache_read_tokens     bigint      NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
  cache_write_tokens    bigint      NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
  thinking_tokens       bigint      NOT NULL DEFAULT 0 CHECK (thinking_tokens >= 0),
  cost_micro_usd        bigint      NOT NULL DEFAULT 0 CHECK (cost_micro_usd >= 0),
  cost_basis            text        NOT NULL CHECK (cost_basis IN ('provider', 'list_price', 'subscription_equivalent')),
  context_used_tokens   bigint      NOT NULL DEFAULT 0 CHECK (context_used_tokens >= 0),
  context_window_tokens bigint      NOT NULL DEFAULT 0 CHECK (context_window_tokens >= 0),
  last_provider         text        NULL,
  updated_at            timestamptz NOT NULL
);

CREATE TABLE hub.quota_snapshot (
  owner_id     uuid        NOT NULL,
  "window"     text        NOT NULL CHECK ("window" IN ('five_hour', 'seven_day') OR "window" ~ '^weekly:[ -~]{{1,64}}$'),
  used_percent real        NOT NULL CHECK (used_percent >= 0),
  resets_at    timestamptz NOT NULL,
  severity     text        NULL,
  fetched_at   timestamptz NOT NULL,
  device_id    uuid        NOT NULL,
  PRIMARY KEY (owner_id, "window")
);

-- 2.9 Audit and read log ---------------------------------------------------------------------------------

CREATE TABLE hub.audit (
  seq         bigint      PRIMARY KEY CHECK (seq >= 1),
  at          timestamptz NOT NULL CHECK (date_trunc('milliseconds', at) = at),
  actor_id    uuid        NULL,
  actor_kind  text        NOT NULL
              CHECK (actor_kind IN ('user', 'device', 'session', 'hub', 'operator', 'anonymous')),
  action      text        NOT NULL CHECK (action ~ '^[a-z_]+(\.[a-z_]+)*$' AND length(action) <= 96),
  result      text        NOT NULL CHECK (result IN ('ok', 'denied', 'rejected', 'duplicate', 'failed')),
  org_id      uuid        NULL,
  wg_id       uuid        NULL,
  task_id     uuid        NULL,
  session_id  uuid        NULL,
  device_id   uuid        NULL,
  target_kind text        NULL CHECK (target_kind ~ '^[a-z_]{{1,32}}$'),
  target_id   text        NULL CHECK (target_id ~ '^[ -~]{{1,128}}$'),
  detail      jsonb       NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(detail) = 'object'),
  event_id    uuid        NULL,
  prev_hash   text        NULL CHECK (prev_hash ~ '^[0-9a-f]{{64}}$'),
  hash        text        NOT NULL UNIQUE CHECK (hash ~ '^[0-9a-f]{{64}}$'),
  CHECK ((seq = 1) = (prev_hash IS NULL))
);
CREATE INDEX audit_org ON hub.audit (org_id, seq DESC) WHERE org_id IS NOT NULL;
CREATE INDEX audit_actor ON hub.audit (actor_id, seq DESC) WHERE actor_id IS NOT NULL;
CREATE INDEX audit_task ON hub.audit (task_id, seq DESC) WHERE task_id IS NOT NULL;
CREATE INDEX audit_session ON hub.audit (session_id, seq DESC) WHERE session_id IS NOT NULL;
CREATE INDEX audit_event ON hub.audit (event_id) WHERE event_id IS NOT NULL;

CREATE TABLE hub.audit_checkpoint (
  hub_key_id   text        NOT NULL CHECK (hub_key_id ~ '^[a-z0-9-]{{1,64}}$'),
  seq          bigint      NOT NULL CHECK (seq >= 1),
  hash         text        NOT NULL CHECK (hash ~ '^[0-9a-f]{{64}}$'),
  published_at timestamptz NOT NULL CHECK (date_trunc('milliseconds', published_at) = published_at),
  signature    bytea       NOT NULL CHECK (octet_length(signature) = 64),
  PRIMARY KEY (hub_key_id, seq)
);

-- Append-only triggers (section 1.2) ---------------------------------------------------------------------

CREATE TRIGGER membership_entry_no_change BEFORE UPDATE OR DELETE ON hub.membership_entry
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER membership_entry_no_truncate BEFORE TRUNCATE ON hub.membership_entry
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.membership_entry ENABLE ALWAYS TRIGGER membership_entry_no_change;
ALTER TABLE hub.membership_entry ENABLE ALWAYS TRIGGER membership_entry_no_truncate;

CREATE TRIGGER platform_entry_no_change BEFORE UPDATE OR DELETE ON hub.platform_entry
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER platform_entry_no_truncate BEFORE TRUNCATE ON hub.platform_entry
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.platform_entry ENABLE ALWAYS TRIGGER platform_entry_no_change;
ALTER TABLE hub.platform_entry ENABLE ALWAYS TRIGGER platform_entry_no_truncate;

CREATE TRIGGER event_no_change BEFORE UPDATE OR DELETE ON hub.event
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER event_no_truncate BEFORE TRUNCATE ON hub.event
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.event ENABLE ALWAYS TRIGGER event_no_change;
ALTER TABLE hub.event ENABLE ALWAYS TRIGGER event_no_truncate;

CREATE TRIGGER task_transition_no_change BEFORE UPDATE OR DELETE ON hub.task_transition
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER task_transition_no_truncate BEFORE TRUNCATE ON hub.task_transition
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.task_transition ENABLE ALWAYS TRIGGER task_transition_no_change;
ALTER TABLE hub.task_transition ENABLE ALWAYS TRIGGER task_transition_no_truncate;

CREATE TRIGGER session_transition_no_change BEFORE UPDATE OR DELETE ON hub.session_transition
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER session_transition_no_truncate BEFORE TRUNCATE ON hub.session_transition
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.session_transition ENABLE ALWAYS TRIGGER session_transition_no_change;
ALTER TABLE hub.session_transition ENABLE ALWAYS TRIGGER session_transition_no_truncate;

CREATE TRIGGER operation_transition_no_change BEFORE UPDATE OR DELETE ON hub.operation_transition
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER operation_transition_no_truncate BEFORE TRUNCATE ON hub.operation_transition
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.operation_transition ENABLE ALWAYS TRIGGER operation_transition_no_change;
ALTER TABLE hub.operation_transition ENABLE ALWAYS TRIGGER operation_transition_no_truncate;

CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON hub.audit
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON hub.audit
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.audit ENABLE ALWAYS TRIGGER audit_no_change;
ALTER TABLE hub.audit ENABLE ALWAYS TRIGGER audit_no_truncate;

CREATE TRIGGER audit_checkpoint_no_change BEFORE UPDATE OR DELETE ON hub.audit_checkpoint
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER audit_checkpoint_no_truncate BEFORE TRUNCATE ON hub.audit_checkpoint
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.audit_checkpoint ENABLE ALWAYS TRIGGER audit_checkpoint_no_change;
ALTER TABLE hub.audit_checkpoint ENABLE ALWAYS TRIGGER audit_checkpoint_no_truncate;

-- Rights (section 1.4) -----------------------------------------------------------------------------------

REVOKE ALL ON ALL TABLES IN SCHEMA hub FROM PUBLIC;
GRANT SELECT ON hub.schema_version TO {writer};
GRANT SELECT, INSERT ON
  hub.membership_entry, hub.platform_entry, hub.event, hub.task_transition, hub.session_transition,
  hub.operation_transition, hub.audit, hub.audit_checkpoint
  TO {writer};
GRANT SELECT, INSERT, UPDATE, DELETE ON
  hub.principal, hub.user_identity, hub.user_settings, hub.identity_bundle, hub.passkey, hub.ceremony,
  hub.browser_session, hub.request_nonce, hub.device, hub.device_presence, hub.device_enrollment, hub.invite,
  hub.org, hub.org_member, hub.wg, hub.wg_access, hub.wg_device, hub.platform_role, hub.scope_epoch, hub.key_wrap,
  hub.scope_head, hub.device_stream, hub.task, hub.session, hub.vm, hub.operation, hub.dedupe_slot,
  hub.whiteboard_entry, hub.session_ping, hub.notification, hub.credential, hub.device_request,
  hub.device_response, hub.metric_sample, hub.metric_rollup, hub.usage_counter, hub.quota_snapshot
  TO {writer};
