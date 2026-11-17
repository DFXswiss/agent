# Agent sync protocol

This document is the contract between the `agent` client and this hub.

## Identity

Anyone may sign in with GitHub. Authorization is not the login; it is team membership.

- A user always reads and writes their own store.
- A user listed on a team in `teams.yaml` also reads every member of that team.
- Membership changes only through a pull request to `teams.yaml`.
- The optional top-level `agents` map assigns owners to agent GitHub logins. Agent and owner logins are lowercase, the agent must belong to a team, and every owner must share a team with it. Duplicate owners, a key written twice anywhere in the file, self-ownership, and invalid references fail closed.

## Device pairing

1. `POST /pair/prepare` with `{device_id, challenge, device_name}`.
2. Open `/pair?challenge=...` in a browser, sign in, `POST /pair/confirm`. The body may include `as_login` for an agent owned by the signed-in person.
3. Client polls `GET /pair/wait?device_id&challenge` until `{status: "paired", token, login}`.

The token is a device credential. It is not a GitHub token. Normally the hub binds `origin_device_id` to the signed-in GitHub login. An owner may select one of their configured agents on the pair page; the device and token are then bound to that agent. Unknown agents return 404 and non-owners return 403. The device row records the human confirmer as `confirmed_by`, including for normal pairing.

The `web:` device-id prefix is reserved for synthetic website origins. `POST /pair/prepare` and `POST /pair/confirm` reject that prefix case-insensitively; website origins cannot be paired as devices. A bearer for a reserved-prefix device row left from before this rule is treated as an unknown device token.

## Events

`POST /sync/push` with `Authorization: Bearer <device-token>`:

```json
{
  "events": [
    {
      "origin_device_id": "<uuid of this device>",
      "origin_seq": 1,
      "table": "task",
      "op": "insert",
      "row_id": "<uuid>",
      "payload": {},
      "occurred_at": "2026-08-13T12:00:00Z"
    }
  ]
}
```

Rules:

- `origin_device_id` must be the device in the token (otherwise 403).
- Origins beginning with `web:` (case-insensitive) are reserved for website-created rows. A device bearer cannot push them (403); only the hub may author them through website actions.
- `origin_seq` is per device, starts at 1, and must be `last + 1` (otherwise 409).
- Repeating the exact same event is idempotent.
- Repeating a seq with different content is 409.
- Allowed tables: `session`, `activity`, `task`, `task_round`, `agent`, `checklist_item`, `local_check`, `review_gate`, `open_work`, `ping`.
- Allowed ops: `insert`, `update`, `delete`.
- Open session ids are unique. A colliding `session` insert for an id that is already open is 409.
- Foreign `origin_device_id` on push is 403. A push must not steal a replica row owned by another device (same `table`+`row_id`).

### Session mail

An `activity` insert with `payload.type=message` must set `payload.payload.to_session` to the recipient **session id**. The hub resolves that id against the replica.

`activity` rows with `payload.type=assignment.created` or `assignment.reply` are hub-authored only. A device push using either type is refused with 400; devices cannot manufacture assignment links or replies. An identical replay of an event already stored under the same `origin_seq` is still acknowledged first, as for every table.

- Missing target session → 404.
- Sender is not allowed to see the owning login of that session → 403.
- Open session ids are unique; the hub uses that uniqueness to route the snapshot.

Website-created person pings use synthetic origin `web:<login>`. `POST /api/pings/{id}/ack` records a `web:<recipient-login>` event, does not transfer replica ownership of the ping row, and does not consume the sender device's `origin_seq`.

### Pull and restore

Devices do **not** default-pull every visible origin. The website still reads the full visibility-filtered replica (`GET /api/state`, `GET /api/stream`).

`GET /sync/pull?cursor=<origin_id>:<last_seq>&cursor=...`:

```json
{
  "events": [],
  "inbox": [],
  "pings": [],
  "subscriptions": []
}
```

- `events` — this device's own events after the cursor for **this** origin, gapless. Other origins are omitted.
- `inbox` — row snapshots: each `activity` with `type=message` whose `payload.to_session` this device owns, plus the parent `session` snapshot for that activity's `session_id`.
- `pings` — person-ping row snapshots this login **sent or received** (not every team-visible ping).
- `subscriptions` — replica snapshots that pass this device's stored matchers and are visible under §6 (`hub.visible`). Always present (empty list when none). Deduplicated by `(table, row_id)`.

`GET /sync/restore` returns:

```json
{
  "device_id": "<uuid>",
  "login": "<github-login>",
  "own_events": [],
  "inbox": [],
  "pings": []
}
```

A wiped laptop replays `own_events` in `origin_seq` order, then applies `inbox` and `pings` as row snapshots (not a holey event stream). Restore does **not** include a `subscriptions` key.

`GET /sync/ws?token=...` pushes `{type: events|ping|hello|subscription}` when new data arrives, including session-mail for the recipient login. The same socket also accepts inbound control messages (see Control). Clients that never send `control-ready` keep working as before.

### Subscriptions and query

Devices keep a **subscription set on the hub** (per device), not in the event log. Subscriptions are not ledger events.

A **match** object is a JSON object. Evaluation is the AND of all entries. Allowed paths only:

- `type`
- `payload.repo`
- `payload.issue_key`
- `payload.to_session`

Any other key is HTTP **400** `unknown match path`.

Each value is either:

- a string → equality against the dotted path on the replica payload (`type` → `payload["type"]`; `payload.repo` → `payload["payload"]["repo"]` when `payload["payload"]` is a dict)
- `{"in": ["a", "b"]}` → membership; `in` must be a non-empty list of strings

A missing path fails that predicate (the row does not match); it is not 400. Caps: at most 32 subscriptions per device; at most 4 predicates per match; at most 16 strings in each `in`. Exceed → 400.

`PUT /sync/subscriptions` (device bearer) replaces the whole set in one transaction:

```json
{ "subscriptions": [ { "match": { "type": "investigate.step" } } ] }
```

Response 200 echoes `{ "subscriptions": [ { "match": { ... } } ] }` in stored order. An empty list clears. `GET /sync/subscriptions` returns the same body for the calling device.

`POST /sync/query` is a one-shot matcher, not a pull and not stored:

```json
{ "match": { "type": "pr.open", "payload.repo": "owner/repo" } }
```

Response 200: `{ "rows": [ <replica_out>, ... ] }` — only rows whose `github_login` is in `hub.visible(caller)`, only matcher-passing rows, ordered by `table_name`, `row_id`, capped at 500.

When a push materializes a replica row (insert/update), device WebSocket queues may also receive `{ "type": "subscription", "rows": [ <replica_out> ] }` for matchers that pass and visibility that allows it. Browser cookie SSE (`/api/stream`) does not get subscription frames. If the device has no open `/sync/ws` queue, pull catches up. Hub-authored assignment rows are returned by query and subscription snapshots only when their origin is `web:<replica login>`, their inner payload is an object, and its `actor` equals the replica login.

## Control

The hub is a relay only. It does not run tmux, does not start processes, and does not author session rows. Devices publish session rows through the normal push path. Control and terminal bytes ride on top of that.

### Ownership and visibility

- A session is **visible** to a viewer when the replica's `github_login` is in `hub.visible(viewer)` (self plus teammates from `teams.yaml`).
- `can_control(login, origin_device_id)` is true only when the session's origin device row exists, is not revoked, and its `github_login` either equals the actor login or is an agent login that actor owns. Other teammates may watch but may not control.
- Unknown or non-visible session ids return **404** (no existence leak across teams).

### Session state fields

`GET /api/state` keeps its existing payload. Every object in `session` gains two computed (not stored) fields:

- `can_control` — see above.
- `control_connected` — true only while that origin device has an open `/sync/ws` connection that has sent `{ "type": "control-ready" }`. Disconnect clears it.

`GET /api/session?id=<id>` (cookie session required; legacy `GET /api/sessions/{id}` remains supported):

```json
{
  "id": "<row_id>",
  "payload": { },
  "_github_login": "...",
  "_origin_device_id": "...",
  "can_control": true,
  "control_connected": false
}
```

404 if missing or not visible.

### Control POST

`POST /api/session/control?id=<id>` — cookie session **or** device bearer (`Authorization: Bearer`). Legacy `POST /api/sessions/{id}/control` remains supported.

Body (one of):

- `{ "action": "start", "command"?: string, "provider"?: "grok", "model"?: string, "cols"?: int, "rows"?: int }`
- `{ "action": "stop" }`
- `{ "action": "input", "data": string }` **xor** `{ "action": "input", "key": "enter"|"ctrl-c"|"tab" }`
- `{ "action": "resize", "cols": int, "rows": int }`

Validation (400): `action` required and one of those four; `command` if present is a string of length 1..4000; `provider` if present is exactly `grok`; `model` if present is a string of length 1..64 and requires `provider=grok`; `provider` and `command` cannot both be set; `data` if present is a string with utf-8 byte length 1..4096; `key` if present is exactly `enter`|`ctrl-c`|`tab`; input must have exactly one of `data` or `key`; `cols`/`rows` if present are integers 1..500; resize requires both; unknown extra keys are ignored.

`provider=grok` is launch metadata for the owning device. The hub does not run `grok`. The device mints a UUID for Grok `--session-id` (it never forwards the store session id) and later uses `--resume` with `runtime.grok_session_id`. An empty model becomes `grok-4.6` on the device.

Authz:

- 401 if not signed in / bad token
- 404 if session missing or not visible
- 403 if not `can_control`
- 409 with detail `owning device is not control-connected` if the origin device has no live control-ready socket
- 202 `{ "queued": true }` after the hub has queued the control frame on **that** origin device's socket (`device.id == origin_device_id`, never another laptop of the same login)

The hub does **not** call store write helpers and does not insert a `session` (or any) store event for control.

Forwarded WebSocket frame to the device:

```json
{
  "type": "control",
  "session_id": "<id>",
  "action": "start|stop|input|resize",
  "payload": { }
}
```

Payload contents:

- start: include `command` only if provided; include `provider` / `model` only if provided; include `cols`/`rows` only if provided
- stop: `{}`
- input: `{ "data": "..." }` or `{ "key": "enter" }`
- resize: `{ "cols": N, "rows": N }`

### Bidirectional `/sync/ws`

Existing behaviour stays: hello on accept; fan-out of visibility-filtered events via `hub.queues`; keepalive `{ "type": "ping" }` every 20s when idle.

Receive loop on the same socket. A device is control-connected only after it sends `{ "type": "control-ready" }`. Disconnect removes it.

Device → hub (unknown types are ignored; the socket stays open):

```json
{ "type": "control-ready" }
{ "type": "terminal", "session_id": "<id>", "seq": 1, "data": "<base64>" }
{ "type": "control-ack", "session_id": "<id>", "action": "...", "ok": true, "error": "optional" }
```

On `terminal`:

- Reject silently if `session_id` is empty, `seq` is not a positive int, or `data` is not a string.
- Drop if the session replica is missing.
- Drop if the connected device's id is not the session's `origin_device_id` (a device cannot publish another device's terminal).
- Append to an in-memory ring: last 64 chunks per `session_id`, drop oldest; each `data` string max 8192 chars (truncate longer).
- Fan out `{ "type": "terminal", "session_id", "seq", "data" }` to browser terminal subscribers for that session.

Terminal bytes are **ephemeral** and **team-visible** (same visibility class as evidence). They are **not** store events. Restore does **not** replay them. They are never written to SQLite.

### Terminal stream

`GET /api/session/terminal?id=<id>` — cookie session. Legacy `GET /api/sessions/{id}/terminal` remains supported. Both return 404 if the session is missing or not visible.

If `Accept` contains `text/event-stream` (SSE):

- first event: `data: {"type":"hello","session_id":"..."}`
- then any currently buffered ring chunks as `data: {"type":"terminal","session_id","seq","data"}`
- then live chunks
- keepalive comment `: \n\n` every 20s
- on disconnect, unsubscribe

Otherwise JSON snapshot:

```json
{ "session_id": "...", "chunks": [ { "seq": 1, "data": "..." } ] }
```

## Website

Cookie session after GitHub OAuth.

- `GET /auth/me` — login, visible logins, teams, and `owned_agents`. GitHub OAuth is sign-in and GitHub API access (`read:user repo`); the token is not in the cookie.
- `GET /api/prs` — open GitHub pull requests authored or assigned to visible logins. Search uses the signed-in user's OAuth token (scope `read:user repo`), stored in hub sqlite (`oauth_token`) and memory, not in the cookie. `source` is `github` or `none` (signed in, but no token — sign in again). Columns: author, org, repo, number, title, status (`open`|`draft`), url. `truncated` is true when 50 rows were returned.
- `GET /api/state` — materialized rows the caller may see (sessions include `can_control` and `control_connected`; devices include nullable `confirmed_by`). The signed-in website leads with `/api/prs`, then replica tables from `/api/state`. Replica arrays are newest-first (`updated_at` descending, then `row_id`). `devices` is newest-first (`created_at` descending, then `id`).
- `GET /api/session?id=<id>` — one visible session plus control flags. `GET /api/sessions/{id}` remains supported.
- `POST /api/session/control?id=<id>` — start / stop / input / resize (device login or configured agent owner only). `POST /api/sessions/{id}/control` remains supported.
- `GET /api/session/terminal?id=<id>` — JSON ring snapshot or SSE terminal stream. `GET /api/sessions/{id}/terminal` remains supported.
- `GET /session?id=<id>` — the static session page using a query-safe id. The page route is served without a cookie so direct entry can render the signed-out notice; it exposes no session data itself. A missing `id` still serves the page, which shows its **Invalid session id.** error state.
- `GET /api/session-timeline?id=<id>` — the newest 5,000 related replica rows, returned oldest first, with `truncated: true` when capped. A missing website cookie returns 401; the session page renders this as **Signed out — sign in on the dashboard** with a link to `/`. The page fetches the timeline once and does not poll.
- `POST /api/assignments` — create and verify an agent-assigned GitHub issue using the owner's OAuth token.
- `GET /api/assignments?agent=<login>` — newest-first assignment activity. Every row has an `enriched` boolean. At most the newest 50 assignments are enriched best-effort with issue state, assignees, and the newest 20 comments by latest change, within one 10-second monotonic budget per request. Every GitHub enrichment read is capped to the smaller of its normal timeout and the remaining budget, and no read starts with no time left. Comment enrichment reads every page before sorting; a budget expiry, page error, or transport error discards the partial comment list and leaves the row `enriched: false`. Enrichment makes no more GitHub calls for that request after the first transport error or after the budget is spent; all remaining rows have `enriched: false`. Older rows also have `enriched: false`. Enriched comments contain `author`, `created_at`, `updated_at`, `body`, and `url`; a missing `updated_at` falls back to `created_at`, `body` is cut to 4,000 characters, and comments are ordered by `(updated_at, GitHub comment id)` descending.
- `POST /api/assignments/{owner}/{name}/{number}/comments` — reply using an owner OAuth token after a fresh assignee check.
- `GET /api/usage` — lane-token aggregates, newest OpenRouter account snapshots, and unchanged Grok snapshot rows.
- `POST /api/pings` — `{to, kind, task_id?, body?}`; `kind` is `review-request|ping|question`. Target must be visible.
- `POST /api/pings/{id}/ack` — recipient only.
- `GET /api/stream` — server-sent events for the signed-in browser.

### Assignments

`POST /api/assignments` accepts a website cookie or device bearer and this body (`request_id` is optional):

```json
{"agent":"agent-login","repo":"Owner/Name","title":"Work title","body":"Work details","deviation":{"declared":false},"request_id":"12345678-1234-1234-1234-123456789abc"}
```

Assignment and reply bodies containing a line beginning with the reserved `<!-- agent-core-request:` marker prefix are rejected with 400.

The actor must own the agent. A bearer belonging to an agent is refused. Repository names use `Owner/Name`; titles are 1..256 characters and bodies are 1..60000. `deviation` is required and must be exactly `{"declared": false}`: it records the owner's active statement that no deviation from the target repository's contribution rules is authorized. A deviation needs an explicit grant on the issue. Missing `deviation`, any other object shape, or `declared: true` returns 400. When present, `request_id` must match `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`. Every JSON error for a request carrying `request_id` includes `request_id_final`: `true` only when retrying that id is not appropriate (validation or authorization/not-found when no GitHub write may already have happened, fingerprint conflict, a completed request whose replay row is unavailable, or reauthentication before a GitHub write), and `false` for in-progress, validation or authorization/not-found after a possible write, post-write reauthentication, and server failures. A successful response clears the pending id; an error without the field keeps it. A missing stored OAuth token returns 409 with a request to sign in on the website. If GitHub returns 401, the hub removes that actor's token from memory and SQLite and the write returns 409 `sign in on the website again`; a 403 remains a 502 and does not remove the token. The hub creates one issue with the agent in `assignees`, verifies the resulting issue, retries the assignee endpoint once if needed, and returns the issue URL on a final 502 so callers do not recreate it blindly. The issue body gets the attribution line `Assigned through <public URL> by @<actor>.`; when `request_id` is present, the marker line `<!-- agent-core-request:<request_id> -->` follows it as the last line. Before a new create, the hub fingerprints the canonical JSON of normalized `repo`, `title`, `body`, `agent`, and `deviation`, with the repository case-folded for the fingerprint only, then replays an existing `assignment.created` activity for that actor, request id, and fingerprint, or reserves that actor, activity type, and request id while recovering the actor's marker-matched recent GitHub issue and writing. Reservation states are `reserved` for a fresh insert, `taken_over` for an expired pending lease, `busy` for a live pending lease, `conflict` for different content, and `done` for a completed request. A takeover is treated as a possible prior GitHub write, so reauthentication keeps the request id for marker recovery. A concurrent request with the same key returns 409 `request is still in progress; retry shortly`; an id already reserved or completed with a different fingerprint returns 409 `request id was used for different content; reload the assignment list` before any GitHub call. The response and stored `assignment.created` payload use GitHub's canonical repository case, even when the request used different casing. Success is 201, or 200 when an already-recorded request is replayed:

```json
{"repo":"Owner/Name","number":42,"url":"https://github.com/Owner/Name/issues/42","agent":"agent-login","actor":"owner-login"}
```

Reservations persist whether a GitHub write may already have occurred. A write that GitHub answers with a definite HTTP status, or a created issue whose later steps fail, expires the lease at once so a retry with the same `request_id` can take over and recover; only a write without any answer keeps its lease. A takeover or a path that reaches a GitHub write records that state. Recovery-phase reauthentication leaves the reservation pending with an expired, immediately takeable lease, so every later retry keeps the request id for marker recovery.

Each reservation stores its first-reserve time. Marker recovery paginates creator issues or issue comments back to that time and reuses only an object authored by the actor whose body, after removing the marker line, matches the expected posted body. If the bounded search cannot complete because its budget or page limit runs out or a request fails in transport, the hub returns retryable 409 without a GitHub write or activity row; a GitHub HTTP error during recovery returns 502 with its bounded detail (and the issue `url`/`number` once known). Both keep the reservation and answer `request_id_final: false`.

Creation records an `activity` replica with type `assignment.created`; replies record `assignment.reply`. Both inner payloads include `agent` (a reply also keeps the complete `agents` list: every agent assigned to the issue, while `agent` is one the replying owner owns; a reply to a transferred issue is posted to, and recorded with, the repository and number GitHub returns for it), include `issue_key` as `<canonical Owner/Name>#<number>`, use `web:<actor>` origins, and contain no OAuth token. The `assignment.created` inner payload stores `deviation: {"declared": false, "stated_by": "<actor>"}`. When supplied, their inner payload also stores `request_id` and `request_fingerprint`. The ledger/replica write and reservation completion are atomic; a handler that lost its lease returns the busy 409 without an event or publication. Their stored URL is canonicalized to `https://github.com/<owner>/<repo>/issues/<number>`; a reply may retain a valid `#issuecomment-<number>` fragment. `GET /api/assignments` accepts only `assignment.created` rows whose replica login matches both the `web:<login>` origin and the inner `actor`. Assignment enrichment stops making GitHub calls for the request after a 401 and removes the viewer's failed token from memory and SQLite; a 403 leaves the token stored. Agents look up mandates with `POST /sync/query` by matching both `type` and `payload.issue_key`, for example:

```json
{"match":{"type":"assignment.created","payload.issue_key":"Owner/Name#42"}}
```

Every GitHub API request of the assignment, reply and enrichment paths, read or write, is bounded by one total 20-second deadline; enrichment and recovery requests use their smaller remaining budget. Sign-in (OAuth token exchange and user lookup) and the PR list search keep their existing per-phase 20-second timeouts. GitHub transport errors return bounded 502 messages. A 502 whose created issue kept no assignee after the retry carries `assignee_missing: true` with `url` and `number`; the website then locks the form and links the issue. Other failures that carry `url` keep the form open, so the same `request_id` can be sent again.

`POST /api/assignments/{owner}/{name}/{number}/comments` accepts a website cookie or a paired device bearer belonging to an owner of an agent currently assigned to the issue. The body is `{body: 1..60000 chars, request_id?}`; `request_id`, when present, has the same lowercase UUID validation as assignment creation. When `request_id` is present, the GitHub comment ends with the same request marker line. The hub fingerprints normalized `owner`, `name`, `number`, and `body`, with owner and name case-folded for the fingerprint only, then replays a recorded `assignment.reply`, or uses the same reservation, content-conflict, busy, and bounded marker-recovery rules instead of posting twice. Success is 201 `{"url":"..."}` and an already-recorded replay is 200 `{"url":"<stored url>"}`. The response is 403 when the actor owns no agent assigned to the issue, and 409 when the actor's GitHub token must be renewed. An agent's own device bearer does not gain owner rights.

### Usage

`GET /api/usage` is cookie-authenticated and covers visible logins:

```json
{"lanes":[],"accounts":[],"grok":[]}
```

`lanes` groups `lane.run` activity by login, session, provider, and model, counts every run, sums token fields (missing usage counts as zero), and reports the newest run time as `last_at`. `accounts` contains the newest `usage.snapshot` (by `fetched_at`) per login, OpenRouter vendor, and account with USD usage and limit fields. Both lists are newest first by the latest replica change of their rows. `grok` contains the same Grok snapshot row shape previously rendered from `/api/state`, newest first by replica change (`updated_at`, then `row_id`).

### Session timeline

The replica-login trust check applies to every ordinary relation row, including `task_round` and `agent`; only verified hub assignment activities may cross logins.

`GET /api/session-timeline?id=<id>` returns 404 unless the session is visible. It includes the session, its activities and tasks, and task/session-related `task_round`, `agent`, `checklist_item`, `local_check`, and `review_gate` replicas. Ordinary `task`, `activity`, `checklist_item`, `local_check`, and `review_gate` relations are included only when the row replica's `github_login` equals the session replica's login; a visible teammate cannot attach a row to another visible login's timeline by copying its session or task id. It also includes trusted visible `assignment.created` and `assignment.reply` activity rows whose inner `payload.agent` equals the session row's owning `github_login`; for a multi-agent reply, membership in its inner `payload.agents` also targets that owning login. A trusted hub-authored row has origin `web:<replica login>`, an object inner payload, and an inner `actor` equal to the replica login. “Visible” here means the assignment row's replica login is in the requesting viewer's normal visible-login set. Each row has `table`, `row_id`, `at`, and `payload`; `at` uses the first non-empty string from the row's `created_at`, `started_at`, `ran_at`, and `recorded_at`, in that order, then the same keys in the inner `payload`, and finally the replica `updated_at`. When more than 5,000 rows match, including these assignment rows, it keeps the newest 5,000 rows and sets `truncated` to true; the oldest rows are dropped, normally including the session row. Unlike every dashboard list, the returned timeline remains oldest first, with equal `at` values ordered by `row_id` and then `table`.

`GET /session?id=<id>` renders lane runs and tool calls, coordinator task phase and blocker information, checks and gates, and assignment links. For `lane.run`, the renderer reads the inner `payload.role`, `payload.vendor` (with `payload.provider` as a fallback), `payload.model`, `payload.status`, and `payload.usage.input_tokens` / `output_tokens`. The producer's tool array is inner `payload.tools`, with entries shaped as `{"tool": string, "target": string, "error": boolean|string}`; legacy inner `payload.tool_calls` is read only by the endpoint, as described below. Inner `payload.tools_truncated: true` is shown even when the retained tool array is empty. The timeline endpoint adds a normalised `tools` object to each `lane.run` row, `{"calls": [{"tool", "target", "error"}], "truncated": bool}`, and the page renders only that object. The endpoint builds it from inner `payload.tools`, or from legacy `payload.tool_calls` when `tools` is not an array: `tool` comes from `tool` or else `name`, `target` from `target`, else `path`, else `command`; a string `error` is kept unchanged and `true` becomes `"error"`; `status: "error"` is a fallback only for legacy `tool_calls`.
