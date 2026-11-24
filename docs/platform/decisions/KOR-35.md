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
