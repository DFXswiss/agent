-- 0012_billing: usage charges and prepaid credit per organization (KOR-31, ADR-059, 12-billing.md section 3).
--
-- Only new tables: an older hub ignores them, and the release changes nothing until the operator adds a price list.
--
-- - `price_list` [AO]: the versions of the price list (configuration, never code); a charge names its version.
-- - `credit_account`: the settled balance of an organization. Every booking of the platform moves `balance_micro_usd`
--   and `booked_micro_usd` by the same amount, so a balance set by hand (`UPDATE`/`INSERT ... ON CONFLICT`) is the
--   only way the two differ; the job `billing.settle` records the difference as a `manual` credit entry and audits it.
-- - `charge_pending`: charges a push booked and the settle job has not yet taken from the balance (a push never
--   locks `credit_account`); the balance of an org is `balance_micro_usd` minus its pending sum.
-- - `credit_entry` [AO]: the history of credits (start credit, operator grants and sets, hand-set balances), numbered
--   per org (`n`, written while the org's account row is locked).
-- - `charge` [AO]: one row per booking of tokens or runtime, unique per outbox item of the device.
-- - `charge_daily`: the charges per org, UTC day, workgroup, device and kind (one row per device, ADR-037).
-- - `fx_rate` [AO]: the USD/CHF rate per calendar day for the books (owner's decision 19), set by the operator; a
--   correction is a further row of the same day. Every charge and credit entry keeps the CHF equivalent with the rate
--   and its day it was booked with (`fx_n`, `fx_day`, `chf_per_usd_e8`, `amount_micro_chf`), so a correction never
--   changes what was booked. USD is the only currency of prices, balances and the interface (decision 18).

CREATE TABLE hub.fx_rate (
  n              integer     PRIMARY KEY CHECK (n >= 1),
  day            date        NOT NULL,
  chf_per_usd_e8 bigint      NOT NULL CHECK (chf_per_usd_e8 > 0 AND chf_per_usd_e8 <= 10000000000),
  source         text        NOT NULL CHECK (source ~ '^[A-Za-z0-9._:/-]{{1,64}}$'),
  set_at         timestamptz NOT NULL,
  actor          text        NOT NULL CHECK (actor IN ('operator')),
  reason         text        NULL CHECK (char_length(reason) <= 500)
);
CREATE INDEX fx_rate_day ON hub.fx_rate (day, n);
CREATE TRIGGER fx_rate_no_change BEFORE UPDATE OR DELETE ON hub.fx_rate
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER fx_rate_no_truncate BEFORE TRUNCATE ON hub.fx_rate
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.fx_rate ENABLE ALWAYS TRIGGER fx_rate_no_change;
ALTER TABLE hub.fx_rate ENABLE ALWAYS TRIGGER fx_rate_no_truncate;

CREATE TABLE hub.price_list (
  version    integer     PRIMARY KEY CHECK (version >= 1),
  valid_from timestamptz NOT NULL,
  document   jsonb       NOT NULL CHECK (jsonb_typeof(document) = 'object'),
  created_at timestamptz NOT NULL
);
CREATE INDEX price_list_valid_from ON hub.price_list (valid_from, version);
CREATE TRIGGER price_list_no_change BEFORE UPDATE OR DELETE ON hub.price_list
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER price_list_no_truncate BEFORE TRUNCATE ON hub.price_list
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.price_list ENABLE ALWAYS TRIGGER price_list_no_change;
ALTER TABLE hub.price_list ENABLE ALWAYS TRIGGER price_list_no_truncate;

CREATE TABLE hub.credit_account (
  org_id            uuid        PRIMARY KEY,
  balance_micro_usd bigint      NOT NULL,
  booked_micro_usd  bigint      NOT NULL DEFAULT 0,
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE hub.credit_entry (
  org_id            uuid        NOT NULL,
  n                 integer     NOT NULL CHECK (n >= 1),
  at                timestamptz NOT NULL,
  kind              text        NOT NULL CHECK (kind IN ('start', 'grant', 'set', 'manual')),
  amount_micro_usd  bigint      NOT NULL,
  balance_micro_usd bigint      NOT NULL,
  reason            text        NULL CHECK (char_length(reason) <= 500),
  actor             text        NOT NULL CHECK (actor IN ('operator', 'database', 'hub')),
  fx_n              integer     NULL CHECK (fx_n >= 1),
  fx_day            date        NULL,
  chf_per_usd_e8    bigint      NULL CHECK (chf_per_usd_e8 > 0),
  amount_micro_chf  bigint      NULL,
  CHECK ((fx_n IS NULL) = (fx_day IS NULL) AND (fx_n IS NULL) = (chf_per_usd_e8 IS NULL)
         AND (fx_n IS NULL) = (amount_micro_chf IS NULL)),
  PRIMARY KEY (org_id, n)
);
CREATE TRIGGER credit_entry_no_change BEFORE UPDATE OR DELETE ON hub.credit_entry
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER credit_entry_no_truncate BEFORE TRUNCATE ON hub.credit_entry
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.credit_entry ENABLE ALWAYS TRIGGER credit_entry_no_change;
ALTER TABLE hub.credit_entry ENABLE ALWAYS TRIGGER credit_entry_no_truncate;

CREATE TABLE hub.charge (
  id               uuid        PRIMARY KEY,
  org_id           uuid        NOT NULL,
  wg_id            uuid        NOT NULL,
  task_id          uuid        NOT NULL,
  session_id       uuid        NOT NULL,
  device_id        uuid        NOT NULL,
  origin_seq       bigint      NOT NULL CHECK (origin_seq >= 1),
  kind             text        NOT NULL CHECK (kind IN ('tokens', 'runtime')),
  at               timestamptz NOT NULL,
  amount_micro_usd bigint      NOT NULL CHECK (amount_micro_usd >= 0),
  price_version    integer     NOT NULL,
  quantity         jsonb       NOT NULL CHECK (jsonb_typeof(quantity) = 'object'),
  fx_n              integer     NULL CHECK (fx_n >= 1),
  fx_day            date        NULL,
  chf_per_usd_e8    bigint      NULL CHECK (chf_per_usd_e8 > 0),
  amount_micro_chf  bigint      NULL,
  CHECK ((fx_n IS NULL) = (fx_day IS NULL) AND (fx_n IS NULL) = (chf_per_usd_e8 IS NULL)
         AND (fx_n IS NULL) = (amount_micro_chf IS NULL)),
  UNIQUE (device_id, origin_seq)
);
CREATE INDEX charge_org_at ON hub.charge (org_id, at);
CREATE INDEX charge_task ON hub.charge (task_id);
CREATE INDEX charge_session ON hub.charge (session_id);
CREATE TRIGGER charge_no_change BEFORE UPDATE OR DELETE ON hub.charge
  FOR EACH ROW EXECUTE FUNCTION hub.refuse_change();
CREATE TRIGGER charge_no_truncate BEFORE TRUNCATE ON hub.charge
  FOR EACH STATEMENT EXECUTE FUNCTION hub.refuse_change();
ALTER TABLE hub.charge ENABLE ALWAYS TRIGGER charge_no_change;
ALTER TABLE hub.charge ENABLE ALWAYS TRIGGER charge_no_truncate;

CREATE TABLE hub.charge_pending (
  charge_id        uuid   PRIMARY KEY,
  org_id           uuid   NOT NULL,
  amount_micro_usd bigint NOT NULL CHECK (amount_micro_usd >= 0)
);
CREATE INDEX charge_pending_org ON hub.charge_pending (org_id);

CREATE TABLE hub.charge_daily (
  org_id           uuid   NOT NULL,
  day              date   NOT NULL,
  wg_id            uuid   NOT NULL,
  device_id        uuid   NOT NULL,
  kind             text   NOT NULL CHECK (kind IN ('tokens', 'runtime')),
  amount_micro_usd bigint NOT NULL DEFAULT 0 CHECK (amount_micro_usd >= 0),
  quantity         bigint NOT NULL DEFAULT 0 CHECK (quantity >= 0),
  PRIMARY KEY (org_id, day, wg_id, device_id, kind)
);

REVOKE ALL ON hub.fx_rate FROM PUBLIC;
GRANT SELECT, INSERT ON hub.fx_rate TO {writer};
REVOKE ALL ON hub.price_list, hub.credit_account, hub.credit_entry, hub.charge, hub.charge_pending, hub.charge_daily
  FROM PUBLIC;
GRANT SELECT, INSERT ON hub.price_list, hub.credit_entry, hub.charge TO {writer};
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.credit_account, hub.charge_pending, hub.charge_daily TO {writer};

-- The outbox item `runtime` (12-billing.md section 4) gets receipts like every other kind.
ALTER TABLE hub.outbox_receipt DROP CONSTRAINT outbox_receipt_item_kind_check;
ALTER TABLE hub.outbox_receipt ADD CONSTRAINT outbox_receipt_item_kind_check CHECK (item_kind IN (
  'event', 'session_transition', 'vm_state', 'operation_transition', 'usage', 'quota', 'metrics', 'audit', 'wrap',
  'runtime'));
