# RUN-04 decisions (session start, platform model access, journal to events)

Small decisions of the package (05-runner.md §2.2 step 5, §3.2, §6.1 steps 1–5, §6.3, §6.4 runner side, §10.1–§10.3
platform kind; 03-api.md §6.1, §7.1, §7.2, §10; 04-crypto.md §4.3, §5.12; 02-data-model.md §4; ADR-009, ADR-010,
ADR-021, ADR-025). The new extension points and the profile store are ADR-032.

## Modules
1. `sessions/`: `records.py` (local rows, transition rules, `session_transition`), `start.py` (steps 1–5, budget
   marks), `stop.py`, `journal.py` (`HANDLERS`), `consume.py` (journal loop of one VM), `service.py` (per-daemon
   service, reconcile, readers, production wiring), `reconcile.py` (`STEPS`, order 50), `tasks.py` (`guestlink`),
   `desired.py`, `harness.py`, `profiles.py` + `consumers.py`, `extensions.py`, `env.py`.
   `model/selection.py` (catalog, strategies, platform kind), `model/provision.py` (platform key, credential body).
2. One daemon task `guestlink` supervises one journal loop per running VM (started at once for a session start,
   otherwise within 5 s; cancelled when the VM no longer runs; restarted with backoff 1 s → 30 s). The registry has
   no per-VM tasks, so the supervision is inside the task.

## Start (05 §6.1)
3. The runner pushes `vm_assigning`, `vm_ready`, `sandbox_ready` (after `POST /v1/sessions` **and** `PUT …/credential`,
   05 §6.1 step 4) and `agent_starting` itself; the guest journals `sandbox_ready`/`agent_starting` too. Both go
   through `records.advance`, which locks the row, never moves backwards and turns a repeated state into no
   transition, so whichever arrives first is the transition. If the session ended meanwhile (the journal reported
   `failed`, the hub stopped it) the start stops, stops its guest session and reports `ended`; any unexpected error
   of a step (store, extension provider) fails the session with `agent_start_failed` and stops its guest session.
4. Every transition carries `model_access`/`model_id` once chosen, `claude_session_id_present` once known and
   `agent_version` = the Claude Code version of the VM's golden image (`runner.golden.claude_version`). The contract
   of `session_transition` has no `agent_kind`; per ADR-021 it is always `claude-code`, the hub's column default.
5. Step 2 accepts the VM only when the guest says `assigned` to exactly this org and workgroup; anything else
   (unreachable, `unassigned`, another workgroup, a missing token file) is `guest_unreachable`.
6. Failure mapping: `ensure_vm` codes `vm_start_failed`/`capacity_exhausted` pass through, every other VM error is
   `vm_start_failed`; no access for the preference (org device, platform device without a usable key file, an
   explicit `subscription`/`api_key` preference until RUN-12) is `model_access_unavailable`; a guest refusal or an
   unexpected answer of `POST /v1/sessions` or `PUT …/credential` is `sandbox_failed`, an unreachable guest there
   `guest_unreachable`; `agent_start_failed`/`sandbox_failed`/`agent_exited` of the guest arrive through the journal.
   A guest session created before a later step failed is stopped (best effort).
7. Budget marks: monotonic time of each step since step 1 began, logged as `runner.session start_step … ms=…` with the
   path (`running`, `restart`, `pool`, `clone`) and `over_budget=1` against 05 §6.1; the journal adds
   `prompt_delivering` and `working` only (the guest may report `agent_starting` before the start code marks
   `sandbox`). The total to `working` is logged as `runner.session started … total_ms=…`.
8. Path classification: the workgroup's VM row before `ensure_vm` (same ID and `running` ⇒ `running`, same ID
   otherwise ⇒ `restart`), else a former pool VM ⇒ `pool`, else `clone` (a golden replacement is a pool or clone
   path, because the old VM was deleted).
9. Interrupted starts: a local session left in `requested`…`sandbox_ready` without a start task (daemon restart
   during its start) fails with `device_shutdown`; its guest session is stopped (`stop.stop_guest`, independent of
   the local state) when it had a VM, because its agent may already run with the platform credential.
10. Desired sessions: only main sessions (depth 0) are started here (subagents: RUN-11); a session is started only
    when its workgroup is assigned to this device by the verified log **and** its owner (named by the hub) has
    access to that workgroup in the verified log; otherwise it is not started and audited once per daemon
    (`runner.inbound_rejected`, source `assignment`, code `owner_not_member`). A desired session that is neither
    `requested` nor known locally is ignored. Local sessions the hub no longer lists are stopped (no reason), those
    of a workgroup no longer assigned with reason `unassigned`.

## Harness and model access
11. The harness context is built with a copy of `agent_guest.harness.context_markdown` (the guest package runs in
    the VM); a test keeps both byte-identical. Names that have not arrived are `unknown`; if the task or org name is
    missing the start pulls the inbox once, cut off after 0.5 s (failures ignored). A task title is used only from a
    `task.meta` of the session's own workgroup (the task ID comes from the hub).
12. Resource limits of a main session: 05 §4.3 (`MemoryMax=4G` = 4 GiB, `CPUQuota=200%`, `TasksMax=256`,
    `RuntimeMaxSec=8h`). Subagent limits in the context: depth 2, 3 running, 10 per task (05 §13).
13. Catalog: `AGENT_RUNNER_MODEL_CATALOG` when configured, else a built-in copy of the platform part of
    `contracts/model-catalog.example.json` (a packaged file would need a change of the package data in
    `pyproject.toml`, which is FND-00's; RUN-12 owns the full catalog). A test keeps the copy equal to the example.
14. The platform key file must be a regular file of the runner user, not a link, mode 0600 or stricter, one
    printable token; it is read for every provisioning and kept in memory only. The credential carries the
    catalog's platform allow-list with context windows, the provider allow-list and the org's daily budget from
    `device/state.platform_budget` (`spent_today_by_org`, 0 when the org has no entry; `null` = unlimited). A budget
    object that is present but malformed fails closed (limit 0, or spent = limit).

## Journal (05 §6.4, 03 §7.2)
15. One page = one store transaction: every record in its own savepoint, then `guest_cursor`; the guest is
    acknowledged only after the commit. Hence local storage before the network and exactly-once across crashes.
16. A record is acted upon only for a local session (canonical UUID) whose `vm_id` is this VM (B4: a guest never
    speaks for another VM); others are logged (`runner.journal foreign_session`) and skipped. A page that is not
    strictly ascending or has a `seq` beyond the store's range is refused before anything is handled (protocol
    error; the loop restarts with backoff), so a guest can only stall its own VM's journal.
17. Invalid records (`JournalRecordInvalid`, a draft refused by the seal checks, e.g. a terminal chunk over 64 KiB)
    are skipped with a fact line; other errors roll the page back and restart the loop from the stored cursor.
18. `prompt_accepted` ⇒ `working`; transitions carry the device time, never the guest's `at`. A repeated state with a
    new `claude_session_id` updates only the ID (decisions/GST-05.md item 8). A guest may report `failed` only with a
    reason it can know (`sandbox_failed`, `agent_start_failed`, `agent_exited`, `prompt_refused`, `delivery_failed`,
    `runtime_exceeded`, `budget_exceeded`, `oom`); VM, capacity, device, assignment and model-access reasons belong to
    the runner and hub. Every `failed` transition (start, journal, interrupted start, unreachable VM) also writes a
    `session.status` event (`{state, reason}`, `events.fail`) so the chat shows the end. `prompt_refused` writes the audit `session.prompt_refused` (result `rejected`, reason only); its effect on
    the delivery belongs to RUN-05 (observer extension point).
19. Device events: header `scope=wg`, `scope_id = wg = session's workgroup`, `task`, `session` of the session, `ref`
    empty, `ts` = the record's time when it is a valid contract time (else device time), a fresh UUIDv7 as ID; the body is the
    journal body unchanged (03 §10). They are sealed at the first push (`sync.seal`, RUN-02) with the newest held
    epoch key of the workgroup and the device key.
20. `hook.denied` only for a PreToolUse deny with a tool name and a rule number 2–6 (`target_kind=session`, result
    `denied`, detail `{tool, rule}`); the path stays inside the encrypted `session.hook` event.
21. `usage`: one request per record; cost basis `provider` with `cost_micro_usd = round(cost_usd × 10⁶)` (0 when the
    gateway saw no cost); `context_used_tokens = input + cache_read + cache_write`; `provider_outside_allowlist`
    with a provider ⇒ audit `model.provider_outside_allowlist`. List prices and subscription equivalents: RUN-12.
22. `guest.error` ⇒ fact line with the code only; `metrics`, `operation.request`, `platform.query`, `ping.delivered`
    have no handler here (RUN-15, RUN-07, RUN-08, RUN-14) and only move the cursor.

Lock order: `records.advance` takes the outbox's transaction advisory lock before the session row lock, the order
of a journal page (outbox items first, a session row later), so a start transition and a journal page never deadlock;
a start transition waits at most for the current page's commit.

## Stop (05 §6.3)
23. `stopping` (with reason), guest `stop`, `stopped`. A VM that is not running or a guest that does not know the
    session (`not_found`) ⇒ `stopped` at once; an unreachable guest or another refusal leaves `stopping` for the next
    reconcile run. The `session.control` hint and interrupt/suspend are RUN-05.
24. `VmManager.on_unreachable` (VM-02 health pass) fails the VM's active sessions with `guest_unreachable`.
    `touch(vm_id)` is called once per successful start; the idle stop of VM-02 already counts active session states
    and session timestamps, so a call per transition is not needed.

## Outside the package's paths
25. Migration `0002_profile_event.sql` (ADR-032) and, because of it, the RUN-01 tests `test_store.py` and
    `test_cli.py` now count the migration files instead of assuming exactly one (ADR-027 §5 anticipates later
    migrations). ADR index line for ADR-032.
26. The marker `vm` is registered and deselected by `tests/platform/runner/sessions/conftest.py` (only `-m vm`
    collects the real-VM run), so `tests/platform/pytest.ini` and the Makefile stay unchanged.

## Review findings accepted without change
28. Events of a session that already ended are still forwarded: the final transcript output and tool results of a
    stopped session legitimately arrive after `stopped`, and the hub's E7 binds them to this device's session. The
    agents that could run on after a failed or interrupted start are now stopped (items 3, 9).
29. A replaced guest journal (rewind) is read from the start; its records are new records, so a journal re-served
    with the same content would be forwarded twice (no stable record IDs in the guest journal). Usage of several VMs
    of one org against one daily budget (each gateway gets the spent amount at provisioning) is RUN-12's quota work.

## Follow-ups noted by ACC-M0
27. Session user names of two sessions started in the same millisecond: already fixed on the integration branch
    by GST-05 (commit 553dcb38, `unit.user_name` uses all random digits; guest tests). The runner passes the hub's
    session IDs unchanged, so nothing is needed here.
