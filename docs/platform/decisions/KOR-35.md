# KOR-35 – Decisions (volume resilience, ADR-063)

## 1. Design review
An independent review of the ADR draft (fresh context: performance, accessibility, zero-knowledge) found 6 high,
7 medium and 2 low findings. All were taken into ADR-063 except where noted:
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
  the order; migration 0012 adds `event_session_seq (session_id, scope_seq) WHERE session_id IS NOT NULL` for a
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
4. **Usage merge in the store.** The merged item lives in `runner.usage_pending` (migration 0012), written in the
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
