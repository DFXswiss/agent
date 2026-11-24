# ADR-086: Volume resilience – every list is windowed, every read is bounded, a volume gate proves it

Status: Accepted (2026-11-24, after an independent design review; findings and their handling in
`decisions/KOR-35.md`)

## Context
- In production a task with a checked-out repository delivered thousands of file events; the task page built all of
  them at once (more than 18 000 DOM elements in 15 s, 13 GB memory, "page unresponsive"). HOT-05 (web) and ADR-057
  (source) fixed that path. Rule [E-19](../KONZEPT.md#e-19) asks to exclude the **class** of error – "an amount of
  data stalls a view" – not the single case, and to prove it permanently. [E-02](../KONZEPT.md#e-02) and
  [A5](../KONZEPT.md#a-5): no artificial upper bounds, generous settings, a clear message instead of a failure.
  [§6 point 3](../KONZEPT.md#k-6-b3): the current state must be visible on a phone; [§6 point 7](../KONZEPT.md#k-6-b7):
  chat and terminal history are always viewable.
- The inventory of KOR-35 (every view and list of the web client, every list endpoint of the hub, every event source)
  found: only the chat timeline renders a window; the whiteboard board and history, operations, audit, task lists,
  notifications, members, devices, invitations and monitoring render everything they hold. The client pulls every
  page of a scope's events and decrypts them although the store keeps only 5 000 per scope. The terminal keeps every
  chunk and copies the list per chunk. The hub pages most lists, but whiteboard history (up to 10 000 versions),
  invitations (up to 1 000), devices, device candidates and the monitoring overview have no paging. Event sources
  without a count bound: terminal output (a record every 100 ms or 16 KiB), hook records (every `allow`), model usage
  (one per request).
- The audit flood (one `event.append` row per stored event) is decided separately in ADR-066 (KOR-40): one row per
  stored batch and scope for the session's high-volume kinds. This ADR does not change the audit.

## Decision

### 1. One building block for every list in the web client
- `core/lists/window.ts` (pure, 100 % Vitest): a **sliding window** over an ordered list of keys with two modes.
  **Follow** (the window ends at the newest item and moves with new items – the default of chat, session chat and
  terminal while the user is at the end, [§6 point 3](../KONZEPT.md#k-6-b3)) and **anchored** (the window keeps its
  first row while items arrive; any move away from the end switches to it). "Earlier"/"later" move by a page; the
  window never holds more than `maxRows` rows – moving past it drops rows at the other end. A missing anchor falls
  back to the nearest end.
- **Weight, not only rows:** each list sets `maxRows` from the weight of its rows (chat 150, tables 200); rows with
  heavy content (Markdown, patches, diffs, JSON) render it only while the row is near the viewport (an
  `IntersectionObserver` with a margin of one screen) and otherwise a light placeholder of the same text clipped to
  one line; heavy content is built from clipped text only.
- `ui/lists/WindowedList.tsx`: renders only the window as an `<ol>`/`<ul>` of memoized rows (`React.memo` with a
  content comparison per list), rows off screen skip layout and paint (`content-visibility: auto` with an intrinsic
  size), buttons at both ends with the count of what is not shown ("Show 50 earlier entries", "4 812 more entries –
  show"), "Jump to the newest", and – when the window reaches the loaded end and the server has more – "Load more"
  (server paging). Every list has a `listId` from a closed set (`core/lists/ids.ts`). Accessibility: real buttons,
  ≥ 44 × 44 px; after a move, focus goes to the first newly shown row and the position is kept by correcting the
  scroll offset (no reliance on `overflow-anchor`, which iOS Safari lacks); rows carry `aria-setsize`/`aria-posinset`
  of the whole list; the shown range is announced in a polite live region only after a user action, never on live
  inflow.
- **Large single entries** (`ui/lists/Clipped.tsx`, the HOT-05 clip generalised): text is shown at most 20 000
  characters at a time ("Show more", and "Download" for the full text, created in the browser from the decrypted
  value); JSON is pretty-printed first and then clipped; whiteboard diffs show only changed hunks with three lines of
  context, collapsed by default per version.
- **Rules for every row:** no store subscription per row on a global map (one selector per list), no `Intl` object
  constructed per row or render (cached formatters), no per-row work on every render (derived data memoized on the
  list's input, the timeline merged incrementally instead of re-sorted per batch), live writes batched per frame, a
  list subscribes only to the slice it shows (the whiteboard does not re-render on chat events), titles of many tasks
  are pulled once per list, not per row. A source test forbids list markup (`<li>`/`<tr>` produced by `.map`) under
  `web/src/ui` outside the shared component, except fixed small lists named with a reason in the test.
- **Bounded store and loading:** the event store is bounded per scope by count (5 000) **and** by bytes (64 MiB of
  ciphertext and bodies), in two classes – bulk session kinds (output, tool, file, hook, terminal, usage) and
  everything else (task titles, whiteboards, profiles, operations) – so chat traffic never evicts titles or
  whiteboards. Live bulk events of tasks no view shows are not decrypted and not stored (only the cursor advances);
  opening such a task reads its newest page. A view loads at most what it shows plus one page ahead: chat, session
  chat and terminal read the **newest** page first (§2) and page backwards on "earlier"; task lists, audit,
  notifications, operations and whiteboards load a page on "Load more" or when the window reaches the loaded end,
  never all pages in a loop. A title search (decrypted in the browser) scans at most 10 further pages per request and
  says "Searched 2 000 of N tasks – search further".
- **Terminal:** chunks are kept in arrays that grow by push (no copy per chunk); follow mode keeps the newest 8 MiB of
  output in memory and the xterm scrollback is 10 000 lines; history mode loads pages around the chosen position.
  Older output is reloaded from the hub, never lost.

### 2. Hub and contracts: every list endpoint is paged or bounded, additively
- `GET /v1/scopes/{scope}/events` gets `before=<scope_seq>` (newest first, `limit` default 200, max 1000, response
  `next_before`; `after` and `before` exclude each other), served by the existing indexes on
  `(scope, scope_seq)`, `(scope, task, scope_seq)` and `(scope, session, scope_seq)`.
- `GET /v1/whiteboards/{entry}/history`, `GET /v1/orgs/{org}/invites`, `GET /v1/platform/invites`,
  `GET /v1/orgs/{org}/devices`, `GET /v1/workgroups/{wg}/device-candidates`, `GET /v1/platform/enrollments` (its
  embedded platform log): optional `before`/`limit` (or `after` for the log; default 50, max 200, `next_before`).
  Without these parameters they answer exactly as before, with a hard cap (the existing 10 000 and 1 000; 10 000 for
  the device lists and the log) and `truncated: true` when the cap cut the list.
- **Old hub, new client:** the old hub answers an unknown query parameter with `400 request_invalid`. The client
  then repeats the request once without the new parameters and remembers per hub version that they are not
  supported (ascending pages, the same bounded loading).
- `GET /v1/monitoring/overview`: at most 2 000 sessions, the most recently active first, with `sessions_total` and
  `truncated`.
- Lists that carry key material (`/v1/wraps`, catch-up, device wraps) and the membership in `GET /v1/orgs/{org}`
  stay complete by design: a cut would lose keys or members; they grow with workgroups × key epochs and members,
  each an administrative action. A hub test lists every `GET` route with its bound (paged, capped or bounded by
  structure, with the reason); a new `GET` route without an entry fails the suite.
- Writes: the per-event bounds stay (ciphertext ≤ 256 KiB, terminal ≤ 64 KiB, 100 events and 4 MiB per request).
  **The hub never refuses or drops session events by count**: a refusal would make the runner retry forever or lose
  content silently. Counts are bounded at the source (§3) and in the view (§1).
- Live channel: unchanged and already bounded (4 096 frames / 8 MiB per connection, then `resync`; replay ≤ 1 000
  per scope). After `resync` the client reads the newest page of the scopes a view shows and the non-bulk kinds of
  the others (titles, whiteboards, profiles) by their cursors, so nothing is skipped silently.

### 3. Sources: bundle display events, keep every content
- Terminal (guest recorder): a record is written when 100 ms passed and output is pending; under sustained output a
  record grows up to 1 s or until its **encoded** size reaches 60 KiB – the same bytes in far fewer events.
- Hook records with the decision `allow` or `none` for `PreToolUse`/`PostToolUse` are counted per session and written
  as one `session.hook` summary `{event:"summary", decision:"allow", counts:{event: n}, tools:{name: n}, from, to}` at
  most every 10 s and before any other hook record of the session (a `Stop` or a `deny` keeps its place). Deny,
  block, fault and turn-end records stay single (they feed `hook.denied`, the metrics and the subagent results). The
  schema change is additive; rollout order: runner and web client before the guest. An old runner does not refuse
  the new shape - it checks no journal body against the schema, seals the summary as a `session.hook` event like any
  other (it is no PreToolUse deny, so no `hook.denied`; no turn end) and the hub stores the ciphertext - but an old
  web client that validates the decrypted body does not show it. A clean stop of the guest's control process writes
  the pending summaries before the journal closes.
- Model usage: the runner merges the usage deltas of a session over 10 s into one `usage` item – only deltas with the
  same `model` and `access_origin` (the billing key of KOR-31); `at` is the newest merged request's time.
- Tool records and outputs stay one event each (each is a step the user wants to see; their rate is bounded by the
  model); a record that would exceed the ciphertext bound after escaping is clipped field by field with a marker
  instead of being skipped. File events: ADR-057.
- The bounds are settings with generous defaults (`agent_guest/limits.py`, runner config). A test of the agent
  repository lists every event kind a device produces with its bound; a new kind without one fails.

### 4. The volume gate
- Playwright projects `volume` (desktop) and `volume-mobile` (Pixel 7 with 4× CPU throttling) run the specs
  `web/e2e/volume/*.spec.ts` against the real hub of the suite with a reproducible data set seeded by the device
  double: one task with 10 000 items of each chat kind and items at the event bound (256 KiB ciphertext – the largest
  single entry the contracts allow, standing in for "1 MB"; very long lines), 5 000 tasks, 2 000 notifications,
  500 members, 500 operations, 300 whiteboard entries and 200 versions of one entry, a subagent tree beyond the hub's
  depth, devices and invitations at the list caps, audit rows of all of it; plus **live load** (20 sessions in
  other tasks writing 200 events/s while a view is measured) and a **scaling check** for the chat (from 1 000 to 10 000
  items per kind the heap may grow at most 2×, the time until usable at most 3× or by at most one second).
- Every view passes the same budget (`web/e2e/volume/budget.ts`): usable (its primary control visible and enabled)
  within 10 s desktop / 20 s phone; fewer than 6 000 DOM elements; JS heap below 300 MB; at most 300 ms of long
  tasks in a quiet 2 s window and less than 50 % long-task time under live load; scrolling moves the page and a frame
  gap during scrolling stays below 500 ms; the window controls work. Reasons for each number: `decisions/KOR-35.md`.
- **Independent of the device's load, not blunt:** the gate runs on shared, often busy test devices, so a bound must
  neither fail because the device is busy nor be so loose that a real regression passes. Therefore (1) the
  **structural bounds** (DOM elements, JS heap, rows shown after the window controls were used five times) are
  never scaled – a list that renders everything breaks them on any machine; (2) the **time bounds** (usable,
  long-task time and share, frame gap) are multiplied by the slowdown of the measured tab, **calibrated in the run**:
  a fixed piece of main-thread work (script, JSON, layout) is timed in the same tab right before the user action and
  after the measurement, and its ratio to the reference time of the idle device (at least 1, at most 8; the phone's
  intended 4× throttling is part of its reference) scales the budgets; (3) inputs are checked on **states**, not
  fixed deadlines (after a wheel input the scroll position must change; the wait grows with the slowdown; the view
  must become quiet – the least busy of up to three 2 s windows counts – and a view that never answers again counts as
  frozen after 20 s instead of hanging the run); (4) the
  volume projects run with one worker each, the phone after the desktop, and in a full run after every other
  project; the phone runs every route, tab and list but not the live-load and scaling checks or the settings
  sections below their screen (they test the client's work per event and per item, already bounded on the desktop;
  `PHONE_EXEMPT`). Before counting, the list's window controls are used five times – more rows than the largest
  window – and no shared list may then show more than 200 rows (`MAX_SHOWN`). The HOT-05 spec `e2e/devices/chat-volume.spec.ts` uses the same calibration (`e2e/volume/speed.ts`).
- **Completeness, statically and at run time:** a Vitest test lists every discovered route pattern (`path` of each
  `screens/*/routes.tsx`), task tab, chat-item kind, settings section, header item and `listId`, and fails unless a
  volume spec names it in its `COVERS`; the global teardown fails unless each named item was actually measured
  (`measure(page, id)`) in both projects. A new view, list or item kind without a volume check fails the gate of
  the package that adds it.
- The gate is part of `npm run test:e2e` and therefore of `make platform-ci` of every package.

### 5. Early warning in operation
- The web client observes long tasks of the main thread (`PerformanceObserver('longtask')`; where the browser lacks
  it, event-loop gaps of a 1 s timer) and counts them per view type – a closed list of route patterns from the
  contracts (e.g. `task.chat`), never an ID: number of blocks over 200 ms, blocked time, longest block bucket.
  At most once per hour, and only when something was counted, it sends `{version, views:{type:{count, blocked_ms,
  max_bucket}}}` **anonymously** (no signature, no cookie) to `POST /v1/telemetry/ui` (`fetch` with `keepalive`).
- The hub validates the closed view list and clamps the values, keeps daily counters per `(day, version, view)`
  only – no user, organisation, device or address – and shows them to platform operators
  (`GET /v1/platform/ui-health`, the monitoring page). The anonymous rate limit per source applies. With very few
  users a day's counter may in practice belong to one person; it still names no person, task or content.

## Consequences
- No list of the web client can render more than `MAX_ROWS` rows or load more than one page ahead of what it shows;
  big single entries cost at most a clip; a new view cannot land without a volume check.
- The volume gate adds minutes to the web gate (seeding once, one spec per view, desktop and phone).
- Contracts change additively (`contracts/changes/KOR-35.md`); old clients and old runners keep working, new clients
  work with an old hub.
- Residual: find-in-page of the browser finds only rendered rows (as in HOT-05); `audit/verify` and the verification
  of the organisation log stay linear in their length (hub CPU and a one-time cost per log change, not a view).

## Alternatives considered
- True virtual scrolling with absolute positioning: smallest DOM, but variable row heights (Markdown, patches) make it
  jumpy, break find-in-page and screen-reader order; the bounded sliding window with `content-visibility` keeps the
  DOM small enough and the semantics intact.
- A hub-side count limit per session that drops or refuses events: contradicts "never silently discard" and blocks
  the runner's outbox.
- Measuring only the chat: would leave the class of error open (E-19).
