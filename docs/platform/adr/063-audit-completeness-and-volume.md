# ADR-063: The audit is complete by construction and records actions, not traffic

Status: Accepted (2026-11-24)

## Context
- The concept requires every action, change and execution to be recorded with actor, time, task, session, workgroup,
  device, target, result and changes ([KONZEPT §6 point 4](../KONZEPT.md#k-6-b4)),
  reads as well ([§8 point 6](../KONZEPT.md#k-8-b6)) and
  blocked attempts ([§10 point 8](../KONZEPT.md#k-10-b8)).
  Rule [E-19](../KONZEPT.md#e-19) asks to exclude the class of
  error, not the single case.
- The concept review (AUD-03, gap L5) found the audit incomplete: six writing routes had no audit row
  (`PUT /v1/me/settings`, `PUT /v1/me/bundles/passkey:*`, `POST /v1/wraps`, `POST /v1/device/wraps`,
  `POST /v1/notifications/read` from a browser, `POST /v1/me/recovery/begin`; also `POST /v1/agent-pairings` and the
  WebAuthn `begin` ceremonies). A refused write left no trace: its rows were written in the route's transaction and
  rolled back with it; only the login, a bad recovery proof, outbox items and operation contents were audited on
  refusal. `GET /v1/stream` (live chat, terminal, monitoring) wrote no read row. The read log was "best effort": a
  queue of up to 10 000 rows written within one second, dropped on overflow and lost when the process ended.
- At the same time the chain grew from 9 600 to 30 700 rows in one hour of operation (checkout tests): one
  `event.append` row per stored event and one `read.<resource>` row per authenticated `GET` (every page view, every
  poll of the web and the runner). An audit that records traffic hides the actions it should prove.
- Constraints: the chain stays one gapless, hash-linked sequence (`02-data-model.md` §2.9); the hub stays
  zero-knowledge (details are metadata only); old rows keep their hashes; old clients and runners keep working against
  the new hub (forward and backward compatible).

## Decision

### 1. Every write request leaves a row – enforced by the request pipeline, not by each route
- The pipeline (`agent_hub.http.pipeline`) runs every `POST`, `PUT`, `PATCH` and `DELETE` that reached a route under
  a request-scoped recorder (`agent_hub.audit.recorder`): `append_audit` notes `(seq, hash, result)` of each row the
  request appends. The response of a write is held until the route has finished (it already is for signed requests),
  then the pipeline asks the database which of the noted rows were committed (one indexed query).
- **Refusal** (status ≥ 400, or an exception of the route): unless a committed row of the request has a result other
  than `ok`, the pipeline appends `request.refused` **in its own transaction, after the route's transaction rolled
  back**: actor and agent of the request, the IDs it named (organization, workgroup, task, session from path and query
  parameters), target = the last path parameter, result `denied` (401, 403, 404), `failed` (≥ 500) or `rejected`
  (other 4xx), detail `{method, route, status, code}` (`route` is the path template, never the path; `code` the error
  code of the answer from the closed list of `contracts/enums/`).
- **Placement of refused rows** (reads and writes): the IDs a refused request named are kept only when they belong to
  an organization the actor is an active member of (looked up from the most specific named ID, like the placement of
  successful reads). Otherwise the row keeps actor and target but no organization, workgroup, task or session: a
  stranger cannot write rows into another organization's audit view, and the operator still sees every refusal in
  the chain.
- **Success** without a committed row: the pipeline appends `request.write` (same fields, result `ok`). That row is the
  audit of a request whose handler declared it an idempotent repeat that changed nothing
  (`agent_hub.audit.recorder.unchanged`: a replayed event, invite, task, operation registration, enrollment, an
  agent's repeated claim, ...) and of the routes of `agent_hub/audit/generic_routes.py:GENERIC` (the WebAuthn `begin`
  ceremonies and `POST /v1/recovery/finish`, which create challenges; the device outbox and the device-request
  responses, whose telemetry and encrypted parts have their own records). A request that stored only the session's
  own events (`recorder.deferred`, §4) needs no pipeline row. For anything else a missing row is a defect: logged and
  counted per route in the process (`agent_hub.http.writelog.UNAUDITED`).
- **Windows**: `request.write` and `request.refused` go through the windows of the read log (§3): the first row of a
  key - actor, route, code, IDs, target, query - is written at once, identical repeats within 300 s are counted in
  `audit.repeat`. A device that pushes telemetry every few seconds or a client that repeats the same refused request
  therefore adds one row and a count per window, not one row per request; a killed process loses only the counts of
  its open windows, never a first row.
- Not audited per request: requests that fail before a principal is known (no or a broken signature, the source's
  rate limit, a body over the limit, an unknown route) – they name no verified actor (a claimed key ID is attacker
  data) and are bounded by the failed-signature limit of the source – and `429 rate_limited` answers, so a flood
  cannot grow the chain beyond the rate limits of the keys and sources. An anonymous request that reached its route is
  audited as `anonymous`. The volume a single key can cause is at most what it caused before this decision (one row
  per distinct request in a window); the limits are those of 03-api.md §1.
- **Completeness test** (`tests/platform/hub/audit_api/test_completeness.py`): every mutating route of the app is
  called once as the principal its class requires with an invalid request and must leave exactly one refusal row
  (`request.refused` or the route's own); a fixture of the hub suite fails every test whose request produced a
  `request.write` for a route not in `GENERIC`, so a new route without its own audit fails the suite the first time
  its success path is tested (100 % coverage makes sure it is).

### 2. The six routes write domain rows
`settings.put` (`{model_access, model_id, locale}`: the new values, metadata already in clear at the hub),
`auth.passkey_bundle` (target the passkey credential), `wrap.put` (`{stored, ignored}`, by a user or a device),
`notification.read` for browsers too, `auth.recovery {step: "rotate_begin"}`, `agent.pairing` (anonymous, target the
pairing). The contracts change additively (`contracts/changes/KOR-40.md`).

### 3. Reads: durable before the answer, repeats counted
- The first authenticated read of a **read key** – action, result, actor kind and ID, agent, device, the four scope IDs,
  the target and a fingerprint of the query (so paging, cursors and filters are distinct keys: reading many pages is
  many rows) – within a window of `READ_WINDOW_SECONDS` (300 s) is appended **before** the response leaves the hub; if
  that write fails the client gets `503 unavailable` instead of the data (no read without a record).
- Every further read with the same key in that window only increments a counter in memory. When the window ends (a
  sweep every 5 s, and at shutdown) a key with repeats gets one row `audit.repeat` with the same IDs and
  `{action, repeats, first_seq, window_s}`; `first_seq` links it to the first row. Distinct keys are bounded
  (`MAX_KEYS`, 50 000): beyond that the oldest windows are closed early – nothing is dropped.
- What can still be lost: the repeat count of windows open when the process is killed (not stopped). The fact that the
  actor read that target in that window is durable; documented as accepted.
- `GET /v1/stream` gets read rows: `read.stream` per opened stream (the organization) and `read.stream.terminal` per
  watched session (its terminal output). The route declaration allows `read_resource` on streaming `GET` routes; the
  rows are written when the stream is accepted, before the first frame; the watched set is fixed for the stream.

### 4. Events: the session's own output is summarised by a job
`event.append` stays one row per event for every kind except the high-volume session kinds `session.output`,
`session.terminal`, `session.tool`, `session.hook` and `session.file`: these are stored without a row of their own,
and the job `audit.event_batch` (`agent_hub.audit.eventbatch`, every 60 s) writes for the new ones of each scope one
row `event.append_batch` per session and author device (target the first event, detail `{scope, count,
first_scope_seq, last_scope_seq, ct_bytes}`). The table `hub.event_audit_cover` (migration 0015) keeps per scope the
head up to which the job has looked; per scope the events commit in `scope_seq` order, so none is skipped or counted
twice, and a hub that stops only delays the rows. The job takes the global audit lock only for its appends at the
end of a pass. Whiteboard events keep their own row (the projection orders them by it). The source limit of file
events (ADR-057) stays.

### 5. Blocked attempts of sessions
Hook denials are audited (`hook.denied`, unchanged). Blocks of the second layer (sandbox) are not observable as such
in the guest without kernel audit support (Landlock logging needs a newer kernel than the golden's 6.8); a tool call
that failed (`PostToolUseFailure`) is journaled and appears in the task history. Recorded as a residual risk.

## Measurements
The same E2E scenarios before and after this decision (decisions `KOR-40.md`, section Measurements): the rows of
the chain per action, taken from the hub database of each run.

## Consequences
- A route can no longer write without leaving a row, and a refusal can no longer vanish in a rollback; the class of
  error is closed by the pipeline and guarded by the completeness test.
- The chain records actions and access, not traffic: repeated identical reads and the streaming output of a session
  are counted, not listed one by one, without a gap in the chain.
- New audit actions: `request.refused`, `request.write`, `audit.repeat`, `read.stream`, `read.stream.terminal`,
  `event.append_batch`, `settings.put`, `auth.passkey_bundle`, `wrap.put`, `agent.pairing`; `auth.recovery` gets the step
  `rotate_begin`. An old web or agent tool reads them as unknown actions (the audit list shows the action name).
- The rollout order is hub first; runners and the web need no change for the audit.
