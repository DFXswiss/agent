-- 0016_quota_unreadable: the subscription of an owner refuses the read of its quotas (KOR-40).
--
-- A device that polls an owner's subscription quota reports an unavailable quota when the usage endpoint refuses
-- the credential (a long-lived setup token may not read quotas). The windows are dropped as before; this row keeps
-- the fact, so `GET /v1/me/quotas` can say "not readable" instead of "not available". A later successful read deletes
-- it.

CREATE TABLE hub.quota_unreadable (
  owner_id   uuid        PRIMARY KEY,
  since      timestamptz NOT NULL,
  device_id  uuid        NOT NULL
);

REVOKE ALL ON hub.quota_unreadable FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.quota_unreadable TO {writer};
