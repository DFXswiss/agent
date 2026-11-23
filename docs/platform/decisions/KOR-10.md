# KOR-10 decisions

Runner corrections from AUD-02: restore below the retention, display after a wipe, heartbeat sessions, budget carry-over,
read limits and GitHub edges.

1. **A hub restored below the pruned range realigns the outbox instead of stopping (NA-02).** Acknowledged items are
   pruned after an hour (KOR-03, `RETAIN_S`). When a hub restored from an older export answers `409 origin_seq_gap`
   with an `expected` the outbox no longer holds, those items are gone on both sides. The runner now takes the hub's
   position, as after a store wipe (DR-01): the stored items from `expected` on (which the restored hub never saw) are
   renumbered without gaps from `expected`, `pushed_origin_seq` becomes `expected - 1`, the sealed events are kept
   (the seal does not bind `origin_seq`; only the storage-key context of the body is sealed again), and the loss is
   reported by one log line `runner.alert outbox_items_lost expected=E lost=N` and one audit `runner.alert`
   `{alert: "outbox_items_lost", value: N}` (target `outbox`/`origin_seq_gap`, metadata only) behind the moved items.
   `N` counts the numbers in `[expected, last]` that are no longer stored. Pushing goes on; no `sync_fatal`, no
   operator. Pending reseals are forgotten (they named old numbers; the hub asks again).
   This supersedes KOR-03 decision 16.
   - **Confirmation.** One refusal does not give items up: the runner first reads `GET /v1/device/state`, and only
     `state: active` with `last_origin_seq == expected - 1` confirms it; otherwise the push is retried like a
     transport error (`runner.sync gap_unconfirmed`). Accepted risk: a hub that serves a confirmed position and later
     gets the lost rows back (a replica that lags and then catches up) would make the next push a fatal
     `origin_seq_conflict`; the hub runs on one PostgreSQL and a restore is the operator's explicit act.
   - **No holes.** `prune` used to delete by age alone, and `created_at` (the writer's transaction start) does not
     follow `origin_seq`, so it could delete item 7 and keep item 6; a restored hub asking for 7 would have been
     rewound to it for ever. `prune` now deletes only a prefix (nothing above the lowest item younger than
     `RETAIN_S`), and `realign` closes any hole it still meets.
   - **Corrupt rows** met by `realign` stop syncing as `outbox_corrupt` (rolled back, nothing moved), like `_prepare`.
   - Rejected alternative: keeping acknowledged items as long as a restore may reach back. A restore has no upper age
     bound (exports are taken by the operator), and the volume grows with every metric and event item of the device
     (KOR-03 bounded it for that reason); the items are lost because the operator restored an export that does not
     contain them, and that loss is now reported instead of stopping the device.
   - Expectations above `last + 1` or not a positive integer stay fatal (a hub ahead of the device is not a restore).
   - Scope queues (KOR-08) hold items without a number; a realignment moves only numbered items, and released items
     take numbers after the end as before. `subagents/results.py` reads the drafts in one statement (KOR-08), so a
     realignment cannot make it repeat or skip one.
2. **`status` shows the adopted hub position after a wipe (NA-07).** `runner.status.outbox last_origin_seq` is the
   highest number the device assigned, `GREATEST(MAX(origin_seq), pushed_origin_seq)` (a wiped store that adopted the
   hub's position has an empty outbox), and the new field `next_origin_seq` names the number of the next item (the
   rule of `outbox.enqueue`). `pushed_origin_seq` stays the hub's acknowledged position; the backlog counts the stored
   items above it. Review (low): `position=known|unknown` tells whether the store knows the hub's position yet; before
   it does (fresh or wiped store before the first reconnect) the numbers are not final, the items are renumbered when
   the position is adopted. (The finding named `doctor`; the facts are printed by `agent-runner status` of the doctor
   area.)
3. **The heartbeat sends the sessions (NA-09).** The new file `sessions/heartbeat.py` registers the provider of the
   field `sessions` (`heartbeat.py:SECTIONS`, ADR-028): `[{session_id, state}]` of the sessions on the device that have
   not ended, oldest first. 03-api.md section 6.2 asks for it and the hub checks the field already, so the provider is
   the smaller change than declaring the field reserved. The hub does not act on it (session states reach it through
   the outbox transitions). The register entry of agent-core `spec-deviations.md` (§6.2, "open defect") is to be closed
   by KOR-12. The VM test that listed the heartbeat fields now checks its own two providers
   (`tests/platform/runner/vm/test_vm_tasks.py`).
   - **At most 500 sessions**, below the 1000 items the frame contract allows per list: with the 256 VMs of
     `vm/heartbeat.py`, the longest states and the largest counts a frame of 1000 sessions would pass the 64 KiB frame
     limit of both sides and the hub would close the socket on every heartbeat; 500 fit (about 52 KB, a test builds
     the largest frame). The contract's 1000 per list does not fit a 64 KiB frame either (KOR-12).
   - **Only rows the hub accepts**: open states of `records.STATES` in the query, version-7 UUIDs in the result; a
     single odd row would otherwise make the hub refuse every frame.
   - The query reads `runner.session` without an index on `state` (sessions are few per device; one query per 15 s
     and per early VM beat), with `LIMIT`.
4. **The budget carry-over keeps each session's own uncounted cost (NA-10).** The spent amount of a renewal is the
   organization's, so in `max(0, old spent + observed - new spent)` (KOR-03, A-29) the cost of other sessions and
   devices that the hub counted meanwhile displaced this session's own cost that the hub had not counted yet (old 4,
   own 3, others 5, new 9: carried 0). The own part is now kept apart, in three disjoint pieces:
   - **counted by the hub**: `spent_today_by_org` and `last_origin_seq` of `GET /v1/device/state` come from one
     repeatable-read snapshot, and the hub adds a platform `usage` item's cost in the transaction that stores it, so
     the amount holds exactly this device's platform `usage` items up to `last_origin_seq`;
   - **in the runner's outbox, not counted yet**: the runner adds to `spent_usd` the cost of its stored `usage` items
     above that position that the hub will count for the organization (`model/uncounted.py`, the hub's rule
     `platform_work`: platform access, or no known access and provider cost; own-credential sessions never count);
   - **not in the runner's outbox yet**: the runner names the journal cursor of the session's VM in the new optional
     `budget.journal_through` (read after the items, so a page committed in between is in neither piece rather than in
     both); the guest gateway keeps per session its charges with the journal `seq` of their `usage` record
     (`gateway_credentials.Charge`) and carries those above `journal_through`.
   The renewal carries `max(own charges above journal_through, old spent + observed - new spent)` (both lower bounds of
   the true spending, the pieces never overlap); a new day or no budget clears both. Without `journal_through` (a
   provisioning at the start, an older runner) only the old bound applies. The renewal stamp still compares limit and
   amount only: a new journal position alone does not provision again.
   - **Exact amounts**: the runner adds in integer micro-USD and converts like the hub (`Decimal(micro) / 10**6`), so
     an item the hub counts meanwhile leaves the same float (a last-digit difference would look like a smaller amount,
     which the gateway takes for a new day).
   - **Never lower within a hub day**: the amount sent per organization and UTC day of `hub_time` is the highest of the
     day (`uncounted.Marks`, per daemon). Items the hub rejects, a stale desired state whose items were pruned meanwhile
     or an unreadable row would otherwise lower it; the day's highest amount is still a lower bound.
   - **Charges**: one that could not be journaled (`ApiSockError`) is dropped from the own part (whether it reached
     the journal is unknown; dropping never counts twice); a cost without a `usage` record (no record for the route,
     a model ID the journal refuses) reaches no one and stays own; at most 10 000 per session (oldest dropped).
     An unreadable outbox row is skipped, at most 10 000 items are read (lower bounds still).
   - Reviews: a first version stamped the journal position the runner had *acknowledged* (high: the runner acknowledges
     within milliseconds, long before the hub counts); the second counted own-credential items and could lose the
     carry by float rounding or a falling amount (high/medium). All fixed as above.
   - Third review (medium, fixed): the desired state is read once per budget (`access.platform_body` passes one
     snapshot; a reconcile replacing it meanwhile could otherwise carry yesterday's amount into the new day's mark); a
     guest built from contracts before KOR-10 refuses `journal_through` (closed `budget` object): the renewal is sent
     again without it (`credential_without_journal_through`); a store that cannot be read or a body outside the
     contract leaves the hub's amount (`uncounted_unavailable`), so the budget never fails a start or a renewal.
   - Remaining gaps (documented): the day's mark can pair an older (higher) amount with the current journal position,
     so charges between the two positions are carried by neither (lost, never twice); a runner restart forgets the
     marks (a falling amount then looks like a new day to the gateway: cost lost, never twice); the items are opened
     on the event loop (few while the hub is reachable); an API-process restart loses the observed cost and the charges (as before, A-04); a
     renewal taken between the commit of a `usage` record and the moment the gateway notes its `seq` counts that charge
     as own although the runner may have it (microseconds); a guest journal replaced while the API process lives
     (`JournalRewound`: the cursor restarts at 0) would count old charges as own once; the sum is computed per session
     and renewal (up to 10 000 decryptions; per organization and run would do - a later optimisation); the subagent
     pre-check `platform_budget_spent` still uses the hub's amount alone (a lower bound too).
   - Contract: `guest/session-credential.request.json` `budget.journal_through` (agent-core
     `contracts/changes/KOR-10.md`). Files outside the named ownership: `agent_guest/gateway.py` (charge -> journal
     `seq`), `agent_guest/catalog.py` (`Budget.journal_through`), the runner's model area (`uncounted.py`, `access.py`,
     `gateways.py`, `provision.py` types).
5. **A commit without a change is the formal refusal `no_changes` (NB-03).** The pre-check of `github.commit` refuses a
   snapshot whose plan has no added, modified or deleted file (`files_changed == 0`) with `CheckFailed("no_changes")`
   before the dedupe tag: the operation is registered as locally rejected (`local_check: rejected`, no dedupe slot),
   nothing reaches GitHub, the scratch directory is removed. The message names the exported directory (and "below the
   listed paths" when `paths` filtered every change out), so an agent that wrote elsewhere sees where to write. Before, the
   executor sent an empty tree change and GitHub answered `422`, which ended as `upstream_error` (LVT-01). The code is
   added to the closed list (agent-core contract change `contracts/changes/KOR-10.md`, 03-api.md sections 9 and 11;
   the runner's copies in `operations/extensions.py` and `queries/tools/list_operations.py` follow); a formal refusal
   is never retried.
6. **The checkout directory stays in lower case (NB-04).** `repos/<owner>/<name>` in lower case: GitHub names are
   case-insensitive, and so are the runner's allow-list (`operations/settings.py`), the repository list and the dedupe;
   one directory per repository whatever the spelling of `repo` in the parameters keeps checkout and commit of the same
   repository together. The agent is told the path: the checkout result names it (`dir`, contract
   `ops/github.checkout.result`). A test checks out `Octo-Org/Hello`, gets `dir = repos/octo-org/hello` and commits it
   as `octo-org/HELLO`. LVT-01's failed commit came from writing below a path in another spelling, which NB-03 now
   answers with `no_changes` instead of `upstream_error`. 05-runner.md section 12 is to say "in lower case" (KOR-12).
7. **Reads are limited per session (NB-07).** Every read is journaled and becomes an event and an audit row at the hub
   (03-api.md section 8), so a session reading in a loop grew both without bound. The guest's MCP server keeps a token
   bucket per session for the read tools (every tool but `request_operation`, which the operation budget limits):
   burst 120, then 2 per second (`mcp.ReadLimiter`; chosen generously - no measured read rate of working agents
   exists yet, PFM may tune it; the long waits of `get_operation`/`get_subagent_result` count once per call). A read over the limit is the tool error
   `rate_limited` (MCP contract), checked before the arguments; only the first refused read of a stretch is journaled
   (`platform.refused {code:"rate_limited", tool}`, audited by the runner as `read.session.<tool>` `rejected`), the
   rest of the stretch is refused silently (measured once in-process: about 5 million calls of one session in 5 s left
   130 answered reads and 10 journaled refusals). Review: every journaled refusal - also protocol errors, unknown
   tools, `params_invalid` and an oversized body, which are no reads - passes a second bucket of the same size per
   session (`McpServer.refusals`), so no request of a session grows its records faster than the two rates. This
   replaces KOR-07 decision 3 ("no read is dropped"): reads beyond the limit are refused and only the first of a
   stretch is recorded; 03-api.md sections 8 and 12 say so (agent-core and this repository's copy of the spec). The buckets live in the
   API process (one per session; full buckets are dropped above 1024 sessions). Placed in the guest, not the runner:
   the guest answers some reads itself and journals every refusal, so only there can the flood be stopped before it
   becomes records.
8. **The three unjournaled refusals (NB-08), checked one by one.**
   - `get_subagent_result` asked for the session itself: the tool raises `ToolRefused("not_found")`, journaled as
     `platform.refused {code:"not_found", tool:"get_subagent_result"}` and audited by the runner (`ToolRefused` takes a
     code now; `params_invalid` stays its default).
   - A body over 1 MiB: the router refused it with `413` before the handler ran, so the session was not known. `/mcp`
     now lets every length through the router (`ROUTE_MAX_BODY`, the largest `Content-Length` the router parses) and
     checks `MAX_BODY` after the authentication without reading the body (the router closes the connection); the
     refusal is journaled as `platform.refused {code:"size_exceeded", tool:null}`. An unauthenticated request of any
     size is still `401` before the body and journals nothing (it is no read of a session; the auth guard counts it).
   - "`get_notifications` of an inactive session (`unavailable`)": does not occur as a read of a session. A token of a
     session that is no longer active fails the authentication (`401` before the body, `test_stopped_session_is_refused`),
     and `ping.take` only checks that the session exists, so a session that ends between the authentication and the
     take still gets its answer and its `platform.read` record. `unavailable` is answered only when the control process
     cannot be reached, and then nothing can be journaled at all. No change.
   - Contract: `platform.refused` accepts the three codes (agent-core `contracts/changes/KOR-10.md`); the runner's
     parser accepts exactly them (`session_api/journal.py`). Files outside the named ownership with this one change:
     `agent_guest/mcp.py` (limiters, `ToolRefused` code), `agent_guest/api_routes/mcp.py` (size check after the
     authentication), `agent_runner/subagents/extensions.py` (docstring of `TOOL_ERRORS`: the runner's query tools never
     answer `rate_limited`), `agent_runner/operations/extensions.py` and `queries/tools/list_operations.py` (copies of
     the operation error codes), `docs/platform/spec/03-api.md` (the copy of agent-core's spec, kept identical).
9. **Metrics for the scope queues (NB-09).** KOR-08 left the items that wait in the scope queues of the outbox
   (`runner.outbox_held`) visible only in log lines (`scope_held`, `scope_waiting`) and out of the backlog. Two gauges
   of the textfile sink show them: `agent_runner_device_outbox_held` (items) and
   `agent_runner_device_outbox_held_scopes` (waiting scopes), one count over `runner.outbox_held` in the sampler's
   store read (every 15 s, next to `sync_fatal` and the backlog).
   Only the textfile (sink A): the hub's `metrics` item is a contract (sink B) and an operator watches the device's
   textfile and Prometheus scrapes it; a dashboard panel or an alert rule (`deploy/monitoring/`, owned by the
   monitoring packages) is named as follow-up in the package report. 08-monitoring.md lists both (agent-core and this
   repository's copy; the asset test binds the table to the renderer). The fields of `DeviceEntry` keep a default of 0
   so the monitoring sample builder (`deploy/monitoring/agent_monitoring/sample.py`, another owner) stays valid.
