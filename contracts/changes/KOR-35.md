# KOR-35 – contract changes

## Sources: hook summaries and terminal records (stage d, agent repository)

Additive (ADR-086 section 3):

- `guest/journal/record.json`: new `$defs/body_session_hook_summary` `{event:"summary", decision:"allow",
  counts:{PreToolUse?, PostToolUse?}, tools:{<tool name>: n}, from, to}` and `$defs/body_session_hook_record` =
  `oneOf` of `body_session_hook` (unchanged, one hook call) and the summary. The `session.hook` record of the journal
  envelope, `guest/journal/session.hook.json` and `events/objects.json#/$defs/body_session_hook` refer to
  `body_session_hook_record`. The guest counts `PreToolUse` `allow` and `PostToolUse` `none` (without a reason)
  per session and writes one summary at most every 10 s and before any other hook or state record of the session;
  deny, block, fault, `Stop` and every other event stay single records.
- `body_session_terminal`: description only. The 16 KiB bound of the data of a record is replaced by the body JSON
  bound (64 KiB − 16 bytes, the guest writes at most 60 KiB); every event still holds at most 16384 characters.
- Fixtures: `guest/journal/session.hook` and `events/session.hook` `valid/summary.json` and four invalid summaries.

Rollout: the runner (and a web client that renders the summary) before the guest; an old runner passes the summary
through unchanged, an old client that validates the body refuses it. Old guests keep writing single records, which
stay valid.

## Hub: bounded list endpoints and the UI health report (stage c)

Additive (ADR-086 sections 2 and 5). Without the new query parameters every endpoint answers exactly as before; a
list cut by its cap adds `truncated: true`. A hub without them refuses the parameters with `400 request_invalid`; the
web client then repeats the request without them (ADR-086 section 2).

- `api/scope-events-get`: the query `before` and the response's `next_before` are those of develop (KOR-44, which
  added the same newest-first paging); this package adds only the fixtures `request/valid/before.json`,
  `request/invalid/before-*.json` and `response/invalid/next-before-text.json`.
- `api/whiteboard-history`: query `before` (`q_seq`) and `limit` (`q_limit_list`); response `oneOf` of the whole
  history ascending (at most 10 000, optional `truncated: true`) and a page newest first `{events, next_before}`
  (`next_before` a `scope_seq` or null; an empty page is allowed there).
- `api/org-invites-list`, `api/platform-invites-list`, `api/org-devices`, `api/workgroup-device-candidates`: query
  `before` (`q_before`, the opaque cursor of the other lists) and `limit` (`q_limit_list`); response properties
  `next_before` (paged) and `truncated` (`const true`, capped). The device lists' `maxItems` rise from 1 000 to the
  hub's cap of 10 000.
- `api/platform-enrollments`: query `after` (`q_seq`) and `limit` (`q_limit_log`); response property `next_after`
  (paged). The operator CLI reads the log page by page. Without paging the hub answers the whole log while it has at
  most 10 000 entries; a longer log is refused with `400 request_invalid` and the new error detail
  `paging_required: true` (`common.json#/$defs/error`) - never a cut log, which an operator CLI older than the paging
  would verify and extend from a wrong head (review decision, `decisions/KOR-35.md`).
- `api/monitoring-overview`: response properties `sessions_total` and `truncated` (at most 2 000 sessions, the most
  recently active first) and `devices_truncated` (the devices cut at the device list's cap of 10 000); `devices`
  `maxItems` 10 000.
- New endpoints (spec table `tests/platform/contracts/spec/endpoints-kor35.json`): `POST /v1/telemetry/ui` (anon,
  `api/telemetry-ui.*`) and `GET /v1/platform/ui-health` (`platform:inviter`, `api/platform-ui-health.*`, read
  resource `platform.ui_health` → audit action `read.platform.ui_health` with `audit/read.platform.ui_health.json`).
- New closed list `enums/ui-views.json`: the view types (with their route patterns), the fallback `other`, the block
  buckets and the limits the hub applies (review: `task.charges` for develop's charges tab, KOR-31; `pulls` and `task.pulls` for develop's pull-request views, KOR-48). A report's version other than the one the hub serves (its own version, the
  web client's `config.version`) is stored as `other` (review: no `versions_per_day` limit any more).
- Fixtures for every changed and new schema (`valid/paged*.json`, `valid/truncated.json`, `invalid/…`).

## Hub: bounded charge lists of the billing (KOR-31 on develop)

Additive (ADR-086 section 2). Without the new query parameters both endpoints answer as before; a list cut by its
cap adds `truncated: true`. The `total_micro_usd` is always that of the whole workgroup (30 days) or task.

- `api/task-charges`, `api/workgroup-charges`: query `before` (`q_before`) and `limit` (`q_limit_list`); response
  properties `next_before` (paged) and `truncated` (`const true`, capped: 10 000 sessions, 200 tasks). A page is newest
  first by the UUIDv7 ID of the session or task (their creation order, stable while charges arrive); the cursor is the
  opaque cursor of the other lists with the ID as key and time 0.
- `api/org-billing`: response property `credits_truncated` (`const true`) when older credit entries exist than the
  newest 100 the answer holds.
- Fixtures: `valid/paged.json`, `valid/paged-end.json`, `valid/truncated.json`, `valid/first-page.json`,
  `valid/credits-truncated.json` and the matching `invalid/…`.

## Hub: bounded list of waiting workgroups (KOR-41 on develop)

Additive (ADR-086 section 2). `api/platform-waiting`: response property `truncated` (`const true`) when more than
10 000 workgroups wait (the longest waiting are listed). Fixtures `valid/truncated.json`, `invalid/truncated-false.json`.

## Hub: bounded queue of a task (KOR-46 on develop)

Additive (ADR-086 section 2). `api/task-queue`: response property `truncated` (`const true`) when more than 10 000
prompts wait (the oldest are listed). Fixtures `valid/truncated.json`, `invalid/truncated-false.json`.

## UI health views: the session's browser tab (KOR-44 on develop)

`enums/ui-views.json`: the view type `session.browser` (pattern `/o/:org/s/:session#browser`), the browser tab of the
session detail. Additive: a hub that does not know it counts the view as `other`.
