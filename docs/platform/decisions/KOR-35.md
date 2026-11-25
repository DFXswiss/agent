# KOR-35 – Decisions (volume resilience, ADR-086)

## 1. Design review
An independent review of the ADR draft (fresh context: performance, accessibility, zero-knowledge) found 6 high,
7 medium and 2 low findings. All were taken into ADR-086 except where noted:
- High: new client on an old hub (unknown parameters are `400`) → retry without them (§2); rows are not a weight
  measure → `maxRows` per list, heavy content only near the viewport, bounded store by bytes and in two classes (§1);
  live inflow of the whole organisation → bulk events of tasks no view shows are not decrypted, live-load scenario in
  the gate (§1, §4); completeness only per registry and self-declared → route patterns and `listId`s, checked
  statically and against the measurements actually made, plus a source test against hand-made lists, a hub test of
  every `GET` route's bound and an agent test of every event kind's bound (§1, §2, §3, §4); telemetry would be an
  audited per-user request → anonymous, hourly, only when something was counted (§5); data set below the package's
  numbers → the package's numbers, 4× CPU throttling on the phone, a scaling check (§4).
- Medium: follow mode (§1); hook summaries list tool names, rollout order runner → guest (§3); terminal size measured
  encoded (§3); accessibility details (§1); non-Chromium fallback for long tasks, closed view list (§5); missing paths
  (platform enrollments log, timeline re-sort, titles, monitoring tick) taken into §1/§2.
- Not taken: a search inside the windowed list as a replacement for find-in-page (residual, as HOT-05);
  `audit/verify` and org-log verification stay linear (hub CPU or once per change, not a view).

## 2. Delivery order
The package asks for stages (a) inventory and gate, (b) web, (c) hub and contracts, (d) sources. The commits land in
the order (a), (c), (b), (d): the web of (b) uses the paging of (c), and the hub is always rolled out first (it
serves the web build). Each stage is a separate signed commit per repository.

## 3. Hub and contracts (stage c)
- **Two modes per list.** A list endpoint is paged exactly when the request names `before` or `limit` (`after` or
  `limit` for the enrollments log); otherwise it answers as before, cut at its cap, and adds `truncated: true` only
  when the cap cut it. Old clients see the same body as long as nothing is cut.
- **Stable sort keys.** Row lists page by keyset on `(created_at, id)` (invitations) or `(enrolled_at, id)`
  (devices), newest first, ties by the unique ID; the cursor is the b64u of the canonical `{"at","id"}` like the
  other lists (`agent_hub.events.paging`). Event lists (scope events, whiteboard history) page by `scope_seq`, which
  is unique per scope, and return it as an integer `next_before`.
- **Device candidates.** The unpaged answer keeps the ranking of §4.6.2 (online, free slots, free memory, ID) within
  the newest 10 000 enrolments. A page keeps the enrolment order instead: presence and capacity change between two
  requests, so a ranked keyset would skip or repeat devices.
- **Enrollments log.** Paged ascending like `GET /v1/platform/log` (default 200, max 1000, `next_after`) rather than
  50/200: the operator CLI must read the whole log to verify it, and the CLI now reads it page by page with 1 000
  entries per request; a hub without the parameters (`request_invalid`) is answered by one unpaged request, as before.
- **Newest-first events.** The first page names a `before` above the head (the contract's largest value
  9999999999999999 is accepted). A single `kinds` value is queried by equality so that `event_session_kind` yields
  the order; migration 0030 adds `event_session_seq (session_id, scope_seq) WHERE session_id IS NOT NULL` for a
  session's events without a kind filter (otherwise the primary key walks the whole scope). A test reads every
  direction and filter with sequential scans, bitmap scans and sorts disabled and finds an index scan and no sort.
- **Monitoring overview.** "Most recently active" is the hub time of the session's newest state transition (its
  creation before the first one), ties by ID; `sessions_total` counts every session the member sees.
- **UI health.** View names of the right form that the hub does not know count as `other` instead of a refusal: a web
  client may be newer than the hub, and its report must not fail. Malformed names, more than 64 views, counters
  that are not non-negative integers and bodies above 8 KiB are `400 request_invalid`/`413 size_exceeded`; values
  are clamped (`count` 100 000, `blocked_ms` one hour, `max_bucket` 6). To keep an anonymous endpoint from growing
  the table, a day holds at most 20 versions; reports of further versions count under `other`. The table has no
  identity column and is hub-wide, so it is excluded from org exports. The read right is `platform:inviter`, the
  existing platform operator role of the web client; on the hub host `agent-hub ui-health` prints the same rows.
- **Route bounds.** `tests/platform/hub/test_get_bounds.py` lists every `GET` route with `paged`, `capped:<n>` or
  `structural:<reason>`; a paged route must declare `limit` and a cursor in its contract.

## 4. Web core: store, loading, terminal, early warning (stage b)
- **Two classes, each with its own bound.** The event store bounds each class of a scope separately (5 000 events and
  64 MiB each): one budget shared by both would again let chat traffic push out titles once the bulk class fills
  it. Bytes are the ciphertext as received (b64u text) plus the decrypted body (plaintext length, set by the reader);
  a class is chosen by the checked kind, before the check by the raw header's kind.
- **Skipping by the unchecked header.** Whether a live event is skipped is decided on the raw header (`kind`, `task`,
  `session`), before anything is verified. A hub that lies there can only make the client skip an event, which costs
  nothing: the view that opens the task reads its newest page from the hub and verifies it. Pulls a view asks for
  are never skipped. The registry of shown tasks and sessions (`core/store/focus.ts`) is reference-counted and kept
  outside the store's state, so registering a view re-renders nothing.
- **Holes are dropped, not shown.** When a list reads its newest page again because bulk events of its task were
  skipped (or after `resync`), its stored bulk events below that page lie before a gap of unknown size; they are
  removed and "Earlier" reads them again. Non-bulk events (prompts, statuses, operations) are never skipped, so they
  stay. A list therefore never shows a silent gap.
- **`resync`.** The channel pulls only the non-bulk kinds from each cursor to the head – without the 5 000-event
  skip-ahead, because these kinds are written by single actions – and moves the cursor to the head; every mounted
  view then reads its newest page. The list of non-bulk kinds is checked against `contracts/enums/event-kinds.json`
  by a test (a new kind must be placed in one class). `hello` keeps the old catch-up (bounded by 5 000 seqs); its
  bulk events of tasks no view shows are skipped by the reader.
- **First page and old hubs.** The newest page asks `before=9007199254740991` (the largest safe integer, below the
  contract's bound). An old hub answers `400 request_invalid`: the client repeats once without `before`, remembers it
  per signed client (one per loaded configuration, i.e. per hub) and emulates the page with ascending pages over a
  window of 2 000 seqs below `before`, widened at most 5 times while it holds fewer than `limit` events (at most
  about 15 requests for one page; the answer has the same shape).
- **Single pulls stay single pages.** `pullOnce` (profiles, titles of one task, operation events) reads one newest
  page of 1 000; boards read at most 10 newest pages (10 000 events, the hub's history cap); task titles of a list
  are read per workgroup, newest pages first, continuing where the previous call stopped, at most 10 pages per call
  and never per row. A title older than that stays the deterministic title until a later call reaches it.
- **Timeline.** The selector keeps the items of the previous build per `scope_seq` with the entry they came from; a
  change of the events alone turns only new or replaced entries into items, sorts those and merges them into the
  sorted list (linear). A change of the task row or the sessions (subagent nodes, system rows) rebuilds.
- **Early warning.** View names, patterns and buckets are the contract's (`contracts/enums/ui-views.json`); the
  version is `config.version` of `GET /v1/config` (the client has no build version of its own and is served by that
  hub). The monitor starts after the configuration check in `ui/app/boot.tsx`, signed in or not; it is not an org
  job of `core/jobs`.

### W3 → W1: core API for the views
- `core/store/focus.ts`: `show(store, {scope, task?, session?, refresh?}) → release` while a view is mounted (the
  chat does it through `openChat`; the terminal watch does it itself).
- `core/chat/actions.ts`: `chatList(org, wg, task, session?)`, `openChat(app.tasks, list) → release` (shown, newest
  page, again after `resync`), `loadEarlierChat(app.tasks, list)`; `loadChat` reads the newest page of the task chat.
- `core/tasks/pages.ts`: `selectPages(list)` → `{status, code, before, earlier}` (`before !== null`: older events
  exist – show "Earlier"; `earlier`: loading), `openNewest`, `loadEarlier` for any other event list (operations of a
  task, a session's events).
- `core/tasks/list.ts`: `titles(app.tasks, rows)` with rows `{org_id, wg_id, task_id, title_event_id}` – one call per
  list instead of `pullOnce(…, {kinds: ['task.meta'], task})` per row (monitoring, audit).
- `core/net/pages.ts`: `eventsBefore(client, scope, query, before?, limit?)` for any newest-first read.

## 5. Sources: guest and runner (stage d, agent repository)

1. **Terminal: quiet, age, encoded size.** A record is written after 100 ms without new output, at the latest when
   its first event is 1 s old, or as soon as the buffered events fill a record of 60 KiB of body JSON (escapes
   counted, the same measure as the runner's seal check); then only the full records are written and the rest keeps
   growing (`Recorder.take(whole=True)`), so sustained output becomes at most one record per 60 KiB or per second.
   The 16 KiB bound of the data per record is dropped (each event still holds at most 16 KiB): only the encoded size
   bounds the sealed event. Typing echoes still appear after 100 ms.
2. **Hook summaries at the journal's insert.** Every producer (`hooks.py`, `control_routes/hooks.py`, `prompts.py`)
   writes through `Journal.insert` in the control process, so the counting lives there (`hook_summary.py`) and no
   producer changes. Counted: `PreToolUse` `allow` and `PostToolUse` `none` without a `reason` (faults and unreadable
   bodies carry one). A summary is written before any other `session.hook` or `session.state` record of the session
   (the end of a session is a state record), when the first counted call is 10 s old (at the next call or by the
   one-second ticker of `control_routes/hook_summary.py`), or when 256 tool names are counted. `tools` counts the
   `PreToolUse` calls (the allowed tool calls), not both events. The counts follow the journal's transactions
   (restored on a rollback); a restart of the control process loses at most 10 s of counts, never a deny.
   `HookLimits(summary_seconds=0)` turns it off (the hook protocol suites use that).
3. **Contract shape.** `body_session_hook` stays the single call (an existing contract test reads its `event` enum);
   the new `body_session_hook_record` is `oneOf` call / summary and is what the journal envelope, the journal file
   schema and the event body refer to. A refused hook body now names the root (`""`) besides the closest option's
   pointers.
4. **Usage merge in the store.** The merged item lives in `runner.usage_pending` (migration 0029), written in the
   journal page's transaction, so the guest's acknowledgement never covers a delta that only memory holds. The
   newest values of the non-counter fields are what the hub keeps when it applies items one by one, so its totals
   and latest values are unchanged. The platform budget reads outbox items and pending rows in one statement (one
   snapshot). The window is a module setting (`model.merge.WINDOW_S`), not an environment variable (no new runner
   configuration name).
5. **Clipping instead of skipping.** `sessions/clip.py` cuts the largest text field of a `session.tool` or
   `session.output` record to the longest prefix that fits 256 KiB − 16 bytes of body JSON, with the marker
   `[clipped: n more characters]`; a field that cannot make the body fit becomes its marker when that is shorter.
   An output gets `truncated: true`.
6. **Bounds test.** `tests/platform/test_event_bounds.py` discovers the guest journal kinds, the device-authored event
   kinds and the outbox item kinds and requires an entry for each (`agent_guest.limits.KIND_BOUNDS` for the guest).

## 6. Web views: the shared windowed list (stage b)

1. **Follow grows, then slides.** In follow mode the window keeps its oldest row while it grows up to `maxRows` and
   only then drops rows at the oldest end. A window that slid by one row per new item would remount a row per event
   and move the content under a reader who scrolled up a little; growing first keeps HOT-05's behaviour ("the window
   keeps its first row") and the bound. A move that drops rows at the newest end makes the window anchored; "Jump to
   the newest" follows again with the start size.
2. **One state model for both orders.** `core/lists/window.ts` computes in oriented indices (0 = oldest) and mirrors
   for newest-first lists, so chat (newest at the end) and tables (newest at the start) share every rule and test.
   A missing anchor falls back to the nearer end by its last known index; an emptied list starts following again.
3. **Scroll position by snapshot.** The reading position is kept by a class component's `getSnapshotBeforeUpdate`
   (screen position of the first row that stays, before the DOM changes) and a `scrollBy` of the difference after it,
   on the nearest scrollable ancestor or the page – exact without `overflow-anchor` (iOS Safari).
4. **Heavy content stays once shown.** Content built near the viewport (one screen margin, one shared
   `IntersectionObserver`) is not removed again while its row is rendered: removing it above the viewport would move
   the page in browsers without scroll anchoring, and the window already bounds the rows.
5. **Download instead of copy.** "Download the full text" uses a `blob:` URL on a download link; the CSP (03-api.md §1)
   governs fetches and embedded content, not downloads, so no CSP change is needed (checked by the CSP-violation
   listener of the E2E suite).
6. **"Load more" focus.** A loaded page is shown when it arrives; the focus goes to the first newly shown row, or –
   when the window was not full and the page fits into it already – to the first row of the page.
7. **Source test.** `tests/scripts/list-markup.test.ts` flags `<li>`/`<tr>` made in a `.map(` callback and a `.map(`
   inside `<ul>`/`<ol>`/`<tbody>` under `src/ui` outside `src/ui/lists`; fixed small lists are allowed per file with
   their number of places and a reason (navigation, toasts ≤ 3, countdown steps, quota windows, ≤ 10 subagents, the
   chart table bounded by its range's buckets, chips of one row, ≤ 100 transitions of an operation).
8. **Core completed by W1** (W3 had stopped): `formatNumber`/`formatMoney` reuse one `Intl.NumberFormat` per locale and
   kind; the monitoring overview keeps `sessions_total`/`truncated`; the audit action list knows
   `read.platform.ui_health`. The chat and the session chat use `openChat`/`loadEarlierChat` (newest page first,
   "Load more" for older pages); audit and monitoring read titles through `titles()` once per list.
9. **Paged org lists and whiteboards.** Whiteboard rows and histories, org and platform invitations and the org's
   devices are read page by page with the hub's `before`/`limit` (`next_before`) and "Load more"; an old hub that
   refuses the parameters is asked again without them (as `core/net/pages.ts`). The rules for the board's listed
   entries and for the device presence poll are documented in the loaders' module comments.
10. **Work VM runs.** The run list of the session's work VM panel (KOR-29) is a windowed list
   (`session.workvm.runs`, 20 at a time, at most 100), rendered only while its `details` is open.
11. **Audit actions contract.** The rebase had dropped `credit.changed`, `billing.*`, `read.org.billing` and the
   charges reads from `contracts/enums/audit-actions.json`; develop's list is restored with `read.platform.ui_health`
   added, and the web's closed list follows it.

## 7. The volume gate (stage a)
- **Data set** (`scripts/hub_test_volume.py`, built once per run in about 5 minutes on the test device): the numbers
  of ADR-086 §4 where the contracts allow them – 10 000 items of each chat kind in one task (prompts, messages, tool
  calls, files and file groups, denied hooks, status items, operation cards and results, subagent items, work-VM jobs
  and states; about 120 000 events), 1 000 per kind in a second task (scaling check), 10 000 terminal chunks, 5 000
  tasks, 2 000 notifications, 200 members, 150 org devices and 200 open org invitations (below the ADR's numbers, see
  the finding below), 1 000 open platform invitations (the list cap), 300 whiteboard entries on each level, 200 versions of one 64 KiB entry,
  a subagent chain of depth 6 (hub limit 2) with 31 children (hub limit 10), and the audit rows of all of it. The
  largest single entry is an operation result of about 160 KB (about 215 KB ciphertext): the contracts bound every
  text field at 64 KiB and the ciphertext at 256 KiB, so "1 MB" cannot exist; 64 KiB texts and 64 KiB single lines
  stand for "very long lines". Nothing bypasses the encryption: log batches through `append_batch`, events sealed,
  signed and stored through `append_events` in transactions of 2 000; only content-free rows (tasks, sessions,
  operations, notifications, identities, device rows) are inserted directly.
- **Finding: first sign-in into a large org** (measured 2026-11-24): every member, org device and org invitation is an
  entry of the org's membership log. Before the fix of the log verification, the first sign-in of a member took 13 s
  with about 600 entries, 64–105 s with about 950 and more than 5 minutes with about 3 300 (no request for minutes:
  the verification grew faster than linearly); while it lasted the gate ran with 200 members, 150 devices and 200
  invitations. After the fix of the verification (linear, 4 000 entries 0.7 s in Node) the first sign-in with about
  950 entries took 24 s (desktop) and 0.8 s (phone worker); with the full numbers (about 3 300 entries, run of
  2026-11-24 15:37) the first browser sign-in still did not finish within 5 minutes in any worker, so the gate keeps
  200 members, 150 devices and 200 invitations until the browser path is fixed (open finding).
- **Budgets and their reasons**: usable within 10 s – the acceptance bound of the platform (PERF-01 "< 10 s"); the
  phone 20 s with 4× CPU throttling (a mid-range phone). Fewer than 6 000 DOM elements – production failed at 18 000;
  a window of 150 rich rows (about 30 elements each) plus the shell (about 1 500) stays below, HOT-05 measured 4 000
  for plain rows. JS heap below 300 MB after a garbage collection – production reached 13 GB; mobile browsers kill
  tabs from about 0.5–1 GB. At most 300 ms of long tasks in a quiet 2 s window and frame gaps below 500 ms while
  scrolling – the HOT-05 bounds: a view that is shown must not keep computing, and a half-second freeze is visible.
  Less than 50 % long-task time under live load (HOT-05) – input is still handled half of the time. Scaling factor 2
  between 1 000 and 10 000 items per kind – a windowed view is independent of the count; the factor tolerates one
  page of loading and decryption that grows with the data.
- **Independent of the device's load** (orchestrator finding: HOT-05's phone test failed once on a busy device after a
  2 s deadline for a scroll): structural bounds (elements, heap, rows after eight uses of the window controls) are
  never scaled, so a list that renders everything fails on any machine; time bounds are multiplied by the slowdown
  of the measured tab, calibrated in the run (`web/e2e/volume/speed.ts`: about 16–20 ms of script, JSON and layout work on the idle device,
  timed in the same tab before the user action and after the measurement; factor = time / 20 ms, at least 1, at most
  8; the phone's intended 4× throttling is part of its reference); inputs are checked on states (the scroll position
  changes after each wheel input, waited for as long as the slowdown allows); the quiet window is the least busy of
  up to three consecutive 2 s windows (work a view finishes after it is shown, and a busy device, end – a view that
  keeps computing does not; the first runs showed 0–540 ms in single windows of the same views); a page that does
  not answer for 20 s after it was usable is reported as frozen and its tab is replaced (same passkey copied into a
  new virtual authenticator, the recovery code as fallback), so one frozen view does not fail the views after it;
  before counting, `measure` uses the list's window controls five times (no list may then show more than 200 rows) and checks that the view is still there. Rejected: wider fixed budgets (blunt:
  they would let real regressions through on an idle device); a baseline page of the same run (it would have to be as
  heavy as the views and still drift with the load between the two measurements); the browser's own CPU throttling
  as a reference (it does not see the other processes on the device). The volume projects run with one worker each,
  the phone after the desktop and, in a full run, after every other project (`dependencies` in
  `playwright.config.ts`; selected alone with `--project` they start at once). Amended by HOT-21: no dependencies any
  more; both projects run at once in lanes of their own from the start of the run (`decisions/HOT-21.md`). The HOT-05 spec
  `e2e/devices/chat-volume.spec.ts` uses the same calibration for its input, scroll, quiet-window and burst bounds.
- **One user per worker, each view a fresh page**: each worker onboards its own user, joins the volume org as admin
  and signs in once (the slow first verification); every view is then opened with a page load and the passkey unlock
  (fast from then on), so a view that breaks its page cannot spoil the next one. `flow:unlock` measures that unlock. Browser coverage is not collected in the volume
  projects: the precise V8 counters would slow the measured code.
- **Known gaps** (`web/e2e/volume/gaps.ts`): views over budget today are marked `test.fail` with `KOR-35 gap: …`;
  their entries go when the views are fixed. The measurements: `board/reports/KOR-35-bestand.md` (not in the repo).

## 8. Review (independent review of the delivered package)
- **Rebase fallout (A):** the Sessions tab and the invitation list run through `WindowedList` again on top of
  develop's KOR-41 facts and KOR-36 statuses (all test ids kept). The tab reads device presence once per list, not
  per row; `useDevicePresence` reads the paged device list until the asked devices are found, the list ends or the
  hub's cap (10 000 / `LIMIT_MAX` pages) is read; a device not found shows no reachability. Invitation statuses: the
  verified log decides which invitations exist and which were accepted, the hub pages are hints (open, opened,
  registered); while older pages are unread the rows stop at the oldest invitation the read pages name, so none
  appears out of place before "Load more". `OpenInvites` serves device invitations only.
- **Usage merge (B):** deltas merge only under the same (session, `model`, `access_origin`); a change writes the
  pending row out first. `at` is the time of the newest merged request (a request without a valid time keeps the one
  before). Pending rows always count in the uncounted budget, independent of `MAX_ITEMS`.
- **Event store (C):** eviction is oldest `scope_seq` first, not LRU (`touchEvents` had no callers and is removed).
  Every eviction or drop raises the boundary (`before`) of each affected list, so "Earlier" re-reads it; a mounted,
  paged-back list keeps its rows up to twice the bound; the title read position lives in the pages slice, so evicted
  titles are re-read. Rejected: a real LRU touched from every mounted row (a store write per render).
- **Newest-page re-reads (D):** a list's boundary is its `before`; views render only at or above it; a re-read drops
  bulk events below the new page and raises overlapping lists of the scope; a forced refresh during a running read
  queues one fresh read.
- **Old hub (E):** the head is cached per client and scope; when the ascending windows find nothing, a search by
  halves (about log2(head / 2 000) one-event requests) finds the newest older content; an emulated page is empty
  only when nothing older exists.
- **Unpaged enrollments (F):** the full log up to 10 000 entries, beyond that `400 request_invalid` with detail
  `paging_required` – never a cut log, which an old operator CLI would verify and extend from a wrong head.
  Rejected: an unbounded full log. The monitoring overview reports `devices_truncated`; the screen says so.
- **Telemetry (G):** only the version the hub serves is stored by name, every other as `other` (no
  `versions_per_day` any more); the count-then-insert of a day runs under an advisory lock per day; a hub job purges
  days older than 90 days hourly; the anonymous report gets only the generic `request.write` audit row. The web
  client sends without referrer and credentials, the first report of a page at a random moment within the hour, and
  all tabs share one pool of unreported counters and one hourly limit in `localStorage` (the sender empties the
  pool), so no counter is reported twice. `task.charges` (develop's charges tab, KOR-31) joins the view list.
- **Guest (H):** a clean stop writes all pending hook summaries before the journal closes. The summary schema gets
  `minProperties: 1` for `counts`, `propertyNames` (tool-name pattern) and `maxProperties: 256` for both; `tools` may
  be empty (a summary of `PostToolUse` calls only). The allowed contract keywords now include `minProperties`,
  `maxProperties` and `propertyNames`. ADR-086 §3 corrected: an old runner does not refuse a summary (it checks no
  body against the schema and seals it like any `session.hook`); an old web client that validates the body does not
  show it.
- **List markup (I):** the source rule covers every keyed element returned by `.map(` (option, label, fragments,
  components), each allowlisted file with a count and a reason. Dialog choices that grow with the org (create
  workgroup: members and devices; admit: invited accounts; device assignment: candidates) use the searchable
  `ui/lists/Picker.tsx` (at most 20 rows, "type to narrow" note) instead of `WindowedList` – no new list ids that
  would each need a volume spec.
- **Focus (J):** live inflow never drops the row that has focus: the list switches to anchored mode instead
  (`keepRow` in `core/lists/window.ts`).
- **Clean-up (K):** `WINDOW`, `PAGE` and `windowStart` of `core/chat/files.ts` removed (unused).
- **Open:** `GET /v1/tasks/{task}/charges` (KOR-31) has no cap and its tab is allowlisted in the list-markup rule;
  it needs a cap or a windowed list plus a volume check (follow-up).

## 9. The charge lists of the billing (KOR-31 on develop, follow-up of section 8)

- **Hub:** `GET /v1/tasks/{task}/charges` and `GET /v1/workgroups/{wg}/charges` take optional `before`/`limit`
  (`agent_hub.events.paging`; default 50, max 200, `next_before`). Without them they answer as before (sessions by
  their first charge, tasks by total descending) cut at a cap – 10 000 sessions (the contract's `maxItems`), 200 tasks
  (the former silent `LIMIT`) – with `truncated: true` when cut. Both routes are now `paged` in the bounds table.
- **Stable keyset:** a page is ordered newest first by the UUIDv7 ID of the session or task (creation order). The
  aggregates (first charge, total) change while charges arrive and would move rows between pages; the ID never does.
  The cursor is the existing opaque `{"at","id"}` cursor with `at` 0 (one cursor format for every list).
- **Totals:** `total_micro_usd` is always that of the whole task (now summed in SQL, no longer from the listed
  sessions) or workgroup, also on a page or a cut list.
- **Org billing:** its lists stay bounded by structure (price list, 30 days, one row per workgroup of the org, the newest
  100 credit entries). The credit history was cut silently; the answer now adds `credits_truncated: true` and the
  settings section says so. No paging there: an operator action per entry, 100 is the history the section shows.
- **Web:** the tab "Costs" is a `WindowedList` (`task.charges`) over `usePagedList` and `readHubPage`; the total comes
  from the newest page (`HubPage.body`, a new field of the shared page so a list can read other answer fields). A hub
  without paging (KOR-31 without KOR-35) refuses `limit` with `400 request_invalid`; the client reads the whole list
  once (remembered per client with the other bounded lists – both changes ship in the same hub release) and shows the
  truncation note when the hub cut it. The tab's allowlist entry in the list-markup test is gone.
- **Checked, unchanged:** `GET /v1/me/invitees` (develop) is capped at 1 000 rows (contract `maxItems`) and shown in a
  `Picker`; it has no `truncated` flag yet (open). `/v1/users/lookup`, `/v1/users/{user}/identity` and
  `/v1/platform/users/{user}` answer one object.

## 10. Findings of the volume gate: window controls, SPA routes

- **Window after "Load more" (task chat, session chat, notifications; `ui/lists/WindowedList.tsx`).** Cause of "the
  view is gone after the window controls were used" (big task and `scale:chat.1000` after `[earlier, earlier, load
  more]`): the list focused the first row of a loaded page by its *index* (`keys.length − before − 1`, assumed at the
  oldest end). A loaded page may land elsewhere or reshape the oldest rows: the chat is ordered by time but paged by
  scope seq; a run of file changes is keyed by its first file, so a page that adds earlier files to the oldest shown
  group renames it, the anchor disappears and the window falls back to the oldest end; a page of more rows than the
  window then put the index outside the window. `rowElement(…)` was undefined, `.focus()` threw in a layout effect and
  the route error replaced the screen. Fix: "Load more" remembers the keys it had; the move focuses the first row the
  new window shows that was not shown before, else its first row that is new, else nothing (focus stays) – never an
  index outside the window; the focus step skips a row that live inflow rendered with the move dropped again (follow
  window). Rejected: re-keying file groups by their last file (live inflow at the newest end would rename them
  instead); keeping the hub's paging order in the chat (the timeline is by time, 06-web.md §5.2).
- **Notifications:** the same list; the screen-level scale test (2 000 rows, rows of one millisecond in the hub's
  microsecond order, live rows during each page) passes before and after, the out-of-window focus that matches the
  intermittent blank screen is reproduced at list level (live rows in the same render as a move). No change of the
  hub's order or time format.
- **SPA routes (`agent_hub/static_routes.py`):** `/o/{org}/tasks` (KOR-32) was missing, a direct load answered 404.
  Added; `test_spa_routes_are_the_routes_of_the_web_client` compares `SPA_ROUTES` with every `path` of
  `web/src/ui/screens/*/routes.tsx` (parameters unnamed), so a new screen fails the hub tests until the hub serves it.
  Rejected: deriving the list from the build at runtime (the hub must answer without parsing the bundle; a test keeps
  the two in step at no runtime cost).
- **`list:session.workvm.runs`:** the view is right (a collapsed list that exists only while open; the seed has runs).
  The gate's locator `getByTestId('workvm.panel.runs').locator('summary')` also matches the `<summary>` of every run
  row inside the opened list (strict-mode violation, 21 elements) – a gate fix (`:scope > summary`), no product change.

## 11. Sign-in into a big org: the log readers were super-linear

Finding of the volume gate (section 7, "membership below the ADR's numbers"): the first sign-in of a member into an
org with about 950 membership-log entries took 64–105 s, about 3 300 entries more than 5 minutes.

- **Cause.** Not the signatures or hash chains (they were linear, about 0.25 ms per entry), but the rights questions
  the sign-in jobs ask the verified log, and the state copies of the verification itself:
  - `VerifiedLog.epochHolders` (W2, asked by `mayWrap` for every wrap the wrap-verify job checks and every slot of the
    hub's catch-up offer, and by the self-wraps job) asked `canRead` of every principal in every state of the log, and
    `canRead` of a device scanned every assignment (`deviceWgs`): states × principals × assignments, per slot, and
    `mayWrap` also listed and sorted every reader per slot. Measured before the fix (Node, test device): one slot
    19 ms at 250 entries, 245 ms at 500, 2.2 s at 1 000; 50 offered slots 0.7 s / 11 s / 121 s.
  - `credentialLosses` (credential re-wrap job, every sign-in) computed `credReaders` of every state twice: states ×
    principals × assignments again (71 ms / 1.1 s / 12.3 s at 250 / 500 / 1 000 entries for the founder).
  - every entry copied the growing maps of the state (`new Map(state.invites).set(…)`, principals, access,
    assignments): quadratic time and memory in the verification itself (the platform log likewise).
- **Fix, without changing what is verified** (every format check, chain link, signature and rights rule runs as
  before and in the same order; the vectors, the malicious-hub suite and the property test are unchanged and green):
  - *Versioned maps* (`withEntry`/`withoutEntry` in `core/log/state.ts`): all maps of one lineage share an
    append-only store of every write with its version; a state's map is a version of it and reads the latest write of
    a key at or before that version. A write from the newest version appends (O(1) amortized); a write from an older
    version (a second child of one state) copies into a new store first. They read exactly like the copied `Map`
    (lookups, size, iteration order including delete and re-add), checked against `Map` by a randomized test. Used
    by every transition of the membership and the platform log. (A first version with changes over a base folded at
    √n was still O(n·√n) in map writes – 52 vs 109 writes per entry at 500 / 4 000 entries.)
  - *Assignment index* per plain map and per store (`deviceWgs` no longer scans every assignment).
  - *`changedPrincipals`*: the rights of a principal are derived only from its own entry in `principals`, its own keys
    in `access`/`assignments` and the workgroups; `changedKeys` of two versions of a store names the keys written between them.
    `epochHolders` (now memoized per immutable `VerifiedLog`) and the credential-loss replay ask only those principals
    after an entry, everyone when the workgroups or the owner's own rights changed or the maps share no store.
  - `mayWrap` asks `canRead` of the two principals instead of listing and sorting every reader, and computes the epoch
    holders only when the wrapper is neither the recipient nor a reader now.
  - The property test (12 random orgs over every entry type) compares `epochHolders` for every scope and epoch and
    `credentialLosses` for every owner with their definitions; `tests/core/log/volume.test.ts` runs the sign-in work
    on a volume-shaped org (and a platform log of N/2 invitations) for 500 and 4 000 entries and counts it – the rights
    questions (`canRead`, `deviceWgs`) and the map writes (`Map.prototype.set`, which includes the entries a map copy
    writes) – and requires a ratio below 12 for both counts and a bound per entry at 4 000 entries. Gates must not
    depend on the device's load (coordinator rule), so the counts are asserted and the times only printed.
- **Measured** (Node 22 on the test device, the work of `volume.test.ts`: platform log of N/2 entries, org log of N
  entries, a catch-up offer of a slot per reader of both scopes, the holders of an epoch, the credential-loss replay):
  before the fix the catch-up offer alone took 11 s for 50 slots at 500 entries and 121 s at 1 000 (cubic), so the
  benchmark does not finish; after it 500 entries 83 ms (0.17 ms/entry) and 4 000 entries 708 ms (0.18 ms/entry) in
  quiet runs, ratio 8.5 (wrap checks of 4 000 slots 18 ms, holders included; credential losses 50 ms). Counted
  work: 6.7 rights questions and 3.3 map writes per entry at both sizes (ratio 8.0; bounds 10 and 6).
- **Rejected:** a persistent hash trie (no insertion order, O(log n) per access);
  deriving the changed principals from the entry type (couples the readers to every transition rule – a missed case
  would widen or narrow W2 silently; the map diff is generic); caching the imported verification keys (the signature
  checks are already linear and not the bottleneck).

## 12. Numbers on the current develop

develop took ADR-065 (existing GitHub token), ADR-075 (resource choice, KOR-41), ADR-076 (situation view, KOR-39),
ADR-077 (GitHub for every repository), ADR-078 (GitHub identities), ADR-079 (no approval as a precondition) and up to
ADR-085 (browser as a platform operation), hub migrations up to 0029 and runner migrations up to 0028 in the meantime.
The package's decision is ADR-086, its hub migration `0030_ui_health` and its runner migration `0029_usage_pending`
(the next free numbers). The merged usage keeps the person of its `triggered_by` (a delta of another person writes
the pending row first), the task charges' newest page carries the consumption per person. The waiting list of
the operator keeps its cap with develop's `credit_exhausted` rows, the platform invitations their paging with the
switch's own-invitations filter. The rebases kept every feature of develop and
laid the windowed lists on top: the names of every member (ADR-073) in the member, invitation, audit, task, whiteboard,
monitoring and workgroup rows, the settled invitations with "Close invitation" and "Enter name" in the paged
invitation list, the waiting cards (KOR-25) on the incremental chat timeline, the community entries beside the
package's ones, and the resource choice (ADR-075): the whole-organization assignment, capacity details and VMs in the
windowed device rows (the organization chip leads the bounded workgroup chips), the default device of a workgroup in
the windowed workgroup rows, device and VM in the session rows, `assigned_org` and `vms` in the paged device list
(`org_devices` takes the user and the window), the org assignments of the verified log as versioned maps. The situation view (KOR-39) shares the columns of
the windowed task row (`TaskEntry`), the event pull reads the newest page with the operation's `ref`, and the
situation view's own windows are named in the list-markup check.

## 14. Second review

Findings of the second independent review and of the gate run of the package's commit, each fixed test first.

- **Rights of an organization's device (security).** `changedPrincipals` ignored `orgAssignments`: a
  `device_unassign_org` changes nothing else, so the credential-loss replay (`credentialLosses`) and `epochHolders`
  did not ask the device again and the user's credential key was not rotated away from it. The organization
  assignments are now compared like the other maps (a device named there is asked again), and the doc comment lists
  every right and the fields it is derived from (org and default workgroup, workgroups, the principal's own entry and
  keys in `principals`, `access`, `assignments`, `orgAssignments`; `invites`, `epochs`, `epochAuthors`, `pending` grant
  no right; decertified devices are exclusions per call). Another org or default workgroup gives null (everyone).
  `OrgState` keeps every map as a shared map – a plain map is copied when the state is built – so neither the shared
  stores nor the per-store device index can be changed through a map a caller still holds (the separate index of
  plain maps is gone); `epochHolders` answers a set that refuses changes.
- **Property test against the old code.** The random organizations now contain every entry type that changes rights:
  `admit` with user certifications of a platform log, org and certified platform devices assigned to workgroups and to
  the organization (`device_assign_org`/`device_unassign_org`), and the batch a decertification makes owed (a
  `device_decertify` appended to the platform log, then `unassignAll`); every batch is checked by `checkBatch` with
  the platform state of its time. The references (`deviceWgs`, `canRead`, `readers`, `epochHolders`, `mayWrap`,
  `credReaders`, `credentialLosses` with decertifications) are written in the test from the code before KOR-35 and
  share nothing with the shortcuts; `mayWrap` is compared on every slot (scopes, an unknown workgroup, a `cred`
  scope, epochs 0 to current + 1), recipient, signer and three exclusion sets. Without the fix above the test fails
  (random org 8, the credential losses differ).
- **A chat opened under inflow kept nothing.** Only paged-back mounted lists were protected from eviction; a quiet
  chat opened while other tasks had filled the bound lost its fresh newest page in the same `putEvents`. Every mounted
  list now keeps its events at or above its boundary (the `paged` flag is gone), and `dropEvents` (a forced re-read)
  never drops what another mounted list shows.
- **Merged usage across days.** A merged usage item is priced and booked by its `at` (the newest request): deltas of
  another UTC day no longer merge (a delta without a time merges with either). The runner does not know the price
  list versions (`billing.active` only), so a price change inside a 10-s window prices at most those 10 s with the
  newer list (05-runner.md §10.4).
- **Live load must reach the page.** The volume gate's live check counts the `event` frames the page receives on its
  live channel (a tee of the stream) and fails below half of rate × window.
- **Smaller ones.** A focused row whose item is deleted hands focus to the nearest shown row (next, else previous),
  else to the list's first control; `GET /v1/platform/waiting` filters `no_device` rows in SQL (the cap counts kept
  rows, `truncated` is exact); the guest writes the counted hook records under the journal lock as it closes
  (`Journal.close(flush=True)`), so a hook counted by a still running API thread is not lost; `scale:chat` expects
  only the time factor over the bound while it is a known gap – the heap factor stays a hard bound.
- **Gate run.** The attention routes of develop got their bounds (`paged`; the summary is five counters). The first
  load had grown to 222.5 KiB gzip (guard 219 KiB): the long-task early warning is loaded after the start (a lazy
  import in `boot.tsx`), and the steps and actions of the terminal entries moved from the `terminal` slice (which every
  start loads) to `core/terminal/entries.ts`, which only the lazy terminal view imports. The mixed burst of
  `chat-volume.spec.ts` left the page busy for 1.7 s after it: the hub sends one `resync` per oversized frame, and since
  KOR-35 every `resync` re-reads the newest page of every mounted view; a `resync` that arrives while an earlier one
  still waits in the channel's queue is now dropped (the waiting one covers both) – 0 ms busy after the burst again.
  `core/lists/paging.ts` held only a type (no coverage data): `ListPaging` lives in `core/lists/window.ts`.

## 15. GitHub identities of develop (KOR-47)

- **Bounds.** `GET /v1/me/github-identities` and `GET /v1/device/github-identities` are `structural`: every GitHub
  account is connected by an explicit sign-in at GitHub (an own one by the user, a company one by an admin of the org
  on one of its devices), an administrative set like the paired agents; the device list must stay complete, since the
  device needs every account of the owner. The two reads of one record are one object.
- **Lists.** The own accounts (settings), the company accounts (`/o/:org/github`) and the native `<option>`s of the
  task choice and the workgroup default stay plain lists for the same reason (allowed in `list-markup.test.ts`).
- **Views.** `/o/:org/github` is the view type `github` of the UI health report; its volume spec measures the screen
  for an admin of the volume org without seeded accounts (a seeded account would need a sealed record of a device).

## 16. First load within its guard

The paging, eviction and focus code of this package belongs to the first load (the event reader and the live channel
need it) and pushed the initial JavaScript over its guard (06-web.md section 12.2). Instead of raising the guard, the
start chunk sheds modules that only lazily loaded screens need: modules of the first load import small parts of their
own (`org/capacity.ts`, `org/expiry.ts`, `tasks/paged-list.ts`, `account/member-profile.ts`,
`ui/hooks/cachedSelector.ts`) instead of the device, invitation, paging, join and audit modules, which re-export them,
and the credential re-wrap imports the record code only once a rotation is owed (on the current develop the whole
loss and rotation code is a lazily imported module, `credentials/rotation.ts`).

## 17. Agents as sub-principals (develop's ADR-082) in the linear log readers

- **`changedPrincipals`.** An agent has no role, access or assignment of its own; its rights resolve through its user
  (`acting`). So every agent of a named principal is named as well (an index `user -> agents` of the shared store of
  principals, extended as the store grows), an agent added, replaced or removed is named by its own entry, and the
  user that owes a rotation (`agentOwed`, a shared map now) is named with its agents when that changes.
- **Agent lookups.** `activeAgent`/`agentsOf` read the same index instead of scanning every principal per call
  (`remove` and `losers` ask them per entry).
- **Credential losses.** The replay lives in `credentials/rotation.ts`. Whether a user's devices count (its consent,
  the agent that signed its join being revoked) and which of its agents is a reader depend on the user and all its
  agents: the replay asks everything again when the user or one of its agents is named, else only the named devices.
- **Evidence.** The property test adds agents (added, replaced, removed by their user or themselves, acting for an
  admin) and checks the contract directly: a principal `changedPrincipals` does not name has the same rights in both
  states. Without the agent expansion it fails in 7 of 12 random orgs.

## 18. Scaling check of the chat as a hard, stable bound
The time until usable of the chat with 10 000 items per kind against 1 000 was 2.0–2.5× in the gate runs and sat on
the former bound of 2, so the "known gap" (expected failure) flipped with the device's noise and broke the gate in
both directions. The check is now hard and stable: the heap may grow at most 2×, the time until usable at most 3× or
at most one second more (sub-second times are dominated by noise of the shared test device). A regression that makes
the chat load in proportion to its data (10×) still fails it.

## 19. Pull-request views of develop (KOR-48)
develop's pull-request views came after the gate's registries and failed them, as they should:

- **Lists.** The links of a workgroup and of one task grow with the data: they are `WindowedList`s (`pulls`,
  `task.pulls`) over the hub's cursor pages; the row renders without its own `<li>`. The situation-view line names at
  most `LINE_MAX` (3) links of a task and counts the rest with a link to the task's tab (a line inside a windowed
  entry, like the agent tree's `TREE_MAX`). The task choice of the link form keeps to the first page of the
  workgroup's tasks (`PAGE_SIZE`) even when the task list loaded more; the "linked already" list names at most one
  task per epoch the browser holds; the workgroup bar is navigation over the readable workgroups. These three are
  allowed in `list-markup.test.ts` with that reason.
- **Data set.** `pr_links` (1 000): the founder links pull requests through the hub's own `link_by_user` with sealed
  bodies, 3 of 4 to the big task – the workgroup's view and the task's tab both exceed one hub page (100) and the
  window (200); the situation view's `ensureLinks` reads them in 5 of its 10 pages.
- **Registries.** View types `pulls` and `task.pulls` (ui-views contract, web and hub copies), the two SPA routes
  of the hub, the bound `paged` of `GET /v1/workgroups/{wg}/pr-links`. KOR-48 adds no device event kind. The newest-first event pages are those of develop (KOR-44, `backwards`, `next_after` kept), with
this package's index order (one kind is an equality) on top.
