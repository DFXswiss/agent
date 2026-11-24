# KOR-35 – contract changes

## Sources: hook summaries and terminal records (stage d, agent repository)

Additive (ADR-063 section 3):

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

Additive (ADR-063 sections 2 and 5). Without the new query parameters every endpoint answers exactly as before; a
list cut by its cap adds `truncated: true`. A hub without them refuses the parameters with `400 request_invalid`; the
web client then repeats the request without them (ADR-063 section 2).

- `api/scope-events-get`: query `before` (`q_seq`, excludes `after`); the response is a `oneOf` of the unchanged
  ascending shape `{events, next_after, head}` and the newest-first shape `{events, next_before, head}`
  (`next_before`: the `scope_seq` of the last event when older matching events exist, else null).
- `api/whiteboard-history`: query `before` (`q_seq`) and `limit` (`q_limit_list`); response `oneOf` of the whole
  history ascending (at most 10 000, optional `truncated: true`) and a page newest first `{events, next_before}`
  (`next_before` a `scope_seq` or null; an empty page is allowed there).
- `api/org-invites-list`, `api/platform-invites-list`, `api/org-devices`, `api/workgroup-device-candidates`: query
  `before` (`q_before`, the opaque cursor of the other lists) and `limit` (`q_limit_list`); response properties
  `next_before` (paged) and `truncated` (`const true`, capped). The device lists' `maxItems` rise from 1 000 to the
  hub's cap of 10 000.
- `api/platform-enrollments`: query `after` (`q_seq`) and `limit` (`q_limit_log`); response properties `next_after`
  (paged) and `truncated` (the log capped at 10 000 entries). The operator CLI reads the log page by page.
- `api/monitoring-overview`: response properties `sessions_total` and `truncated` (at most 2 000 sessions, the most
  recently active first); `devices` `maxItems` 10 000.
- New endpoints (spec table `tests/platform/contracts/spec/endpoints-kor35.json`): `POST /v1/telemetry/ui` (anon,
  `api/telemetry-ui.*`) and `GET /v1/platform/ui-health` (`platform:inviter`, `api/platform-ui-health.*`, read
  resource `platform.ui_health` → audit action `read.platform.ui_health` with `audit/read.platform.ui_health.json`).
- New closed list `enums/ui-views.json`: the view types (with their route patterns), the fallback `other`, the block
  buckets and the limits the hub applies.
- Fixtures for every changed and new schema (`valid/paged*.json`, `valid/truncated.json`, `invalid/…`).
