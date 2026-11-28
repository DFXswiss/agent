-- 0042_subscription_usage: the tokens each person used on each Claude subscription (ADR-103; 02-data-model.md section
-- 2.8).
--
-- One row per subscription record, five-minute slot, person and session: the person is the one whose instruction
-- caused the requests (`triggered_by` of the `usage` item, else the session's owner), the counts are the sums of the
-- `usage` deltas the device attributed to that subscription (`subscription_id`). Metadata only: no content, no token,
-- no name. The hub keeps a row only when the subscription may serve the session (the owner's primary record for the
-- owner's own sessions, or a further record whose use mirror names the session's organization or the pool). It goes
-- with its record.

CREATE TABLE hub.subscription_usage (
  credential_id      uuid        NOT NULL REFERENCES hub.credential (id) ON DELETE CASCADE,
  owner_id           uuid        NOT NULL,
  user_id            uuid        NOT NULL,
  org_id             uuid        NOT NULL,
  session_id         uuid        NOT NULL,
  slot               timestamptz NOT NULL,
  requests           bigint      NOT NULL CHECK (requests >= 0),
  input_tokens       bigint      NOT NULL CHECK (input_tokens >= 0),
  output_tokens      bigint      NOT NULL CHECK (output_tokens >= 0),
  cache_read_tokens  bigint      NOT NULL CHECK (cache_read_tokens >= 0),
  cache_write_tokens bigint      NOT NULL CHECK (cache_write_tokens >= 0),
  PRIMARY KEY (credential_id, slot, user_id, session_id)
);
CREATE INDEX subscription_usage_user ON hub.subscription_usage (user_id, slot);
CREATE INDEX subscription_usage_owner ON hub.subscription_usage (owner_id);

REVOKE ALL ON hub.subscription_usage FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.subscription_usage TO {writer};
