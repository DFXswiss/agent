-- 0006_usage_daily: the platform model spend of each organization per UTC day (HUB-14, R-P.13, ADR-026, ADR-037).
--
-- `PLATFORM_BUDGET_USD_PER_ORG_DAY` (03-api.md section 2) limits what sessions with `model_access = platform` cost an
-- organization per day of the hub clock (UTC). The outbox item `usage` adds its `cost_micro_usd` delta to the row of
-- the session's org, the receive day and the reporting device in the same transaction as `usage_counter`; the spend
-- of an org is the sum over its devices. One row per device means that pushes of different devices never lock the
-- same row (a push is one transaction; pushes of one device are serialized by its outbox stream).
-- `GET /v1/device/state` (`platform_budget.spent_today_by_org`) and check H4 of the operation registration read
-- today's sum. Rows of past days stay (a small history for operators); they are not a projection of any log.

CREATE TABLE hub.usage_daily (
  org_id         uuid   NOT NULL,
  day            date   NOT NULL,
  device_id      uuid   NOT NULL,
  cost_micro_usd bigint NOT NULL DEFAULT 0 CHECK (cost_micro_usd >= 0),
  PRIMARY KEY (org_id, day, device_id)
);

REVOKE ALL ON hub.usage_daily FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.usage_daily TO {writer};
