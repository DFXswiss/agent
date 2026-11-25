-- 0025_model_fallback: model access with fallback, visible quotas and DFX access on own hardware (KOR-50, ADR-081;
-- 02-data-model.md sections 2.1, 2.4, 2.8 and 3).
--
-- - `user_settings.fallback_own_to_dfx` / `fallback_operator_to_platform`: the user's crossing rules of ADR-081
--   section 1 (defaults off / on); delivered with every session of the owner in the device state.
-- - `task.model_consent`: a person allowed DFX access for the task while its own access is used up (section 5).
-- - `session.wait_until` / `session.dfx_choice`: the wait of a session suspended with `model_quota_exhausted`
--   (section 4), the input of the attention reasons `quota_wait` / `quota_choice`; `task_attention.wait_until`
--   carries the time into the record.
-- - `quota_limit`: the latest observed limit of an owner's own subscription (`quota.limited`, section 6).
-- - `operator_quota`: the newest snapshot of the operator subscriptions of each platform device (section 6).
-- - `dfx_issuer`: platform devices that announced a management key (`dfx_key_state` with `issuer` true, section 7).
-- - `dfx_key`: one row per key request of an org device and organization: the request, the issued key's reference,
--   its sealed envelope (ciphertext to the org device; the hub never sees the key), the highest cumulative provider
--   spend booked, the revocation and the end of the row. At most one row per org device and organization is not
--   revoked; a revoked key stays listed for the issuer until its final spend is booked (`closed_at`).
-- - `dfx_key_cost`: the provider cost an org device's sessions reported on their per-device key since its last
--   booking (the weights of the split, section 7).
-- - `charge`: a booking of a per-device key splits one outbox item into several rows (`part`, unique with the item);
--   the share no session reported stays with the organization and has no workgroup, task or session (`tokens`
--   only). `charge_daily` keeps such a share under the nil workgroup ID (`agent_hub.billing.outbox.NO_WG`).
-- - `session.wait_at`: the device time of the latest change of the wait (a transition or the item `session_wait`);
--   an older `session_wait` changes nothing.
-- - The outbox items `operator_quota`, `dfx_key_state`, `dfx_key_issued`, `dfx_key_usage` and `session_wait` get
--   receipts like every other kind.

ALTER TABLE hub.user_settings ADD COLUMN fallback_own_to_dfx boolean NOT NULL DEFAULT false;
ALTER TABLE hub.user_settings ADD COLUMN fallback_operator_to_platform boolean NOT NULL DEFAULT true;

ALTER TABLE hub.task ADD COLUMN model_consent boolean NOT NULL DEFAULT false;

ALTER TABLE hub.session ADD COLUMN wait_until timestamptz NULL;
ALTER TABLE hub.session ADD COLUMN dfx_choice boolean NOT NULL DEFAULT false;
ALTER TABLE hub.session ADD COLUMN wait_at timestamptz NULL;

ALTER TABLE hub.task_attention ADD COLUMN wait_until timestamptz NULL;

CREATE TABLE hub.quota_limit (
  owner_id   uuid        PRIMARY KEY,
  at         timestamptz NOT NULL,
  until      timestamptz NULL,
  readable   boolean     NOT NULL,
  device_id  uuid        NOT NULL,
  fetched_at timestamptz NOT NULL
);

CREATE TABLE hub.operator_quota (
  device_id   uuid        PRIMARY KEY,
  at          timestamptz NOT NULL,
  snapshot    jsonb       NOT NULL CHECK (jsonb_typeof(snapshot) = 'array'),
  received_at timestamptz NOT NULL
);

CREATE TABLE hub.dfx_issuer (
  device_id    uuid        PRIMARY KEY,
  announced_at timestamptz NOT NULL
);

CREATE TABLE hub.dfx_key (
  request_id       uuid        PRIMARY KEY,
  org_id           uuid        NOT NULL,
  device_id        uuid        NOT NULL,
  requested_at     timestamptz NOT NULL,
  issuer_device_id uuid        NULL,
  key_ref          text        NULL CHECK (key_ref ~ '^[A-Za-z0-9_-]{{8,128}}$'),
  envelope         text        NULL CHECK (envelope ~ '^[A-Za-z0-9_-]+$' AND char_length(envelope) <= 8192),
  issued_at        timestamptz NULL,
  booked_micro_usd bigint      NOT NULL DEFAULT 0 CHECK (booked_micro_usd >= 0),
  booked_at        timestamptz NULL,
  revoked_at       timestamptz NULL,
  closed_at        timestamptz NULL,
  CHECK ((issued_at IS NULL) = (key_ref IS NULL) AND (issued_at IS NULL) = (issuer_device_id IS NULL)),
  CHECK (envelope IS NULL OR issued_at IS NOT NULL),
  CHECK (closed_at IS NULL OR revoked_at IS NOT NULL)
);
CREATE UNIQUE INDEX dfx_key_live ON hub.dfx_key (org_id, device_id) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX dfx_key_ref ON hub.dfx_key (key_ref);

CREATE TABLE hub.dfx_key_cost (
  device_id      uuid   NOT NULL,
  session_id     uuid   NOT NULL,
  org_id         uuid   NOT NULL,
  cost_micro_usd bigint NOT NULL CHECK (cost_micro_usd >= 0),
  PRIMARY KEY (device_id, session_id)
);
CREATE INDEX dfx_key_cost_org ON hub.dfx_key_cost (org_id, device_id);

ALTER TABLE hub.charge ADD COLUMN part integer NOT NULL DEFAULT 0 CHECK (part >= 0);
ALTER TABLE hub.charge ALTER COLUMN wg_id DROP NOT NULL;
ALTER TABLE hub.charge ALTER COLUMN task_id DROP NOT NULL;
ALTER TABLE hub.charge ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE hub.charge ADD CONSTRAINT charge_scope_check CHECK (
  (wg_id IS NULL) = (task_id IS NULL) AND (wg_id IS NULL) = (session_id IS NULL)
  AND (wg_id IS NOT NULL OR kind = 'tokens'));
ALTER TABLE hub.charge DROP CONSTRAINT charge_device_id_origin_seq_key;
ALTER TABLE hub.charge ADD CONSTRAINT charge_device_id_origin_seq_part_key UNIQUE (device_id, origin_seq, part);

-- `model.switched`: the task's owner learns that a session changed its model access (ADR-081 section 5); replaced
-- NOT VALID like in 0010, 0013, 0021 and 0023.
ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'task.attention', 'invite.accepted', 'rotation.required',
  'agent.account_change', 'org.admitted', 'operator.request', 'device.needed', 'model.switched'))
  NOT VALID;

ALTER TABLE hub.outbox_receipt DROP CONSTRAINT outbox_receipt_item_kind_check;
ALTER TABLE hub.outbox_receipt ADD CONSTRAINT outbox_receipt_item_kind_check CHECK (item_kind IN (
  'event', 'session_transition', 'vm_state', 'operation_transition', 'usage', 'quota', 'metrics', 'audit', 'wrap',
  'runtime', 'operator_quota', 'dfx_key_state', 'dfx_key_issued', 'dfx_key_usage', 'session_wait'));

REVOKE ALL ON hub.quota_limit, hub.operator_quota, hub.dfx_issuer, hub.dfx_key, hub.dfx_key_cost FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.quota_limit, hub.operator_quota, hub.dfx_issuer, hub.dfx_key,
  hub.dfx_key_cost TO {writer};
