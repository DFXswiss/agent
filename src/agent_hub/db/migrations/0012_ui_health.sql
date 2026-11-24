-- 0012_ui_health: volume resilience of the hub (KOR-35, ADR-063 sections 2 and 5; 02-data-model.md sections 2.8
-- and 3).
--
-- `event_session_seq` serves the newest-first pages of a session's events (`GET /v1/scopes/<scope>/events
-- ?session=&before=`) in index order: `event_session_kind (session_id, kind, scope_seq)` yields that order only for
-- one kind, and the primary key `(scope_id, scope_seq)` would walk the whole scope to find one session's events.
--
-- `ui_health_daily` holds the anonymous UI health reports of the web clients (`POST /v1/telemetry/ui`): one row of
-- counters per UTC day of the hub clock, web client version and view type (a closed list of route patterns,
-- `contracts/enums/ui-views.json`). No user, organization, device, address or ID of an object is stored. `reports`
-- counts the reports that named the view, `blocks` the main-thread blocks over 200 ms, `blocked_ms` their summed
-- duration and `max_bucket` the largest bucket of the longest block (`buckets_ms` of the enum file). Platform
-- operators read the rows (`GET /v1/platform/ui-health`, `agent-hub ui-health`).

CREATE INDEX event_session_seq ON hub.event (session_id, scope_seq) WHERE session_id IS NOT NULL;

CREATE TABLE hub.ui_health_daily (
  day        date     NOT NULL,
  version    text     NOT NULL CHECK (version ~ '^[0-9A-Za-z][0-9A-Za-z.+_-]{{0,63}}$'),
  view       text     NOT NULL CHECK (view ~ '^[a-z]+(-[a-z]+)*(\.[a-z]+(-[a-z]+)*)?$' AND length(view) <= 64),
  reports    bigint   NOT NULL CHECK (reports >= 1),
  blocks     bigint   NOT NULL CHECK (blocks >= 0),
  blocked_ms bigint   NOT NULL CHECK (blocked_ms >= 0),
  max_bucket smallint NOT NULL CHECK (max_bucket >= 0),
  PRIMARY KEY (day, version, view)
);

REVOKE ALL ON hub.ui_health_daily FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.ui_health_daily TO {writer};
