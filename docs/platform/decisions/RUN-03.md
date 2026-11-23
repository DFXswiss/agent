# RUN-03 decisions (platform enrollment, reconcile engine, log and wrap verification, pull)

Small decisions of the package (05-runner.md §2.1 platform part, §2.2 steps 1–3, §2.3 pull, §2.4; 03-api.md §6.1,
§6.2; 04-crypto.md §5.9, §6.1–§6.4; 02-data-model.md §4; ADR-013, ADR-023, ADR-027 §4, ADR-028). The new extension
point for inbox consumers is ADR-039; the founder pinning of platform devices is ADR-040.

## Modules
1. `orgs/`: `book.py` (`OrgBook`: verified platform and membership copies, certification state, rights library for
   RUN-04 ff.: `acting_orgs`, `assignments`, `pull_scopes`, `org_of_wg`, `state_at`, `author_may_write`,
   `assigned_at`, `excluded`), `keys.py` (scope keys sealed with the storage key), `audit.py` (`runner.inbound_rejected`,
   log alerts, counters `verify_failures` and `log_alerts` for RUN-15), `db.py` (store sessions, item 20).
   `reconcile/`: `engine.py`, the steps `state.py`, `logs.py`, `wraps.py`, the shape checks `wire.py` and the
   registrations `reconcile.py` (STEPS), `tasks.py`, `hints.py`, `connect.py`. `inbox/`: `pull.py`, `dispatch.py`
   (ADR-039), `tasks.py`, `hints.py`, `connect.py`. `enroll/`: `platform.py`, `cli.py`.
2. Step order numbers are `10 × n` for step n of 05 §2.2 (state 10, logs 20, wraps 30; assignments 40, sessions 50,
   prompts 60, requests/pings/credentials 70, catch-up 80 for the later packages).

## Enrollment (05 §2.1, 04 §5.9)
3. `--hub` is validated with the rules of `AGENT_RUNNER_HUB_URL` (`https://`, loopback `http://` only with
   `TEST_MODE=1`, no path) and recorded in `device.json`; `AGENT_RUNNER_PLATFORM_ROOT_KEY` is required.
4. `GET /v1/config` is anonymous and not response-signed (03 §1); the runner reads it without redirects or proxy,
   at most 1 MiB, and uses only `platform_root_key` (must equal the configured key, else `platform_root_mismatch`,
   fatal) and `hub_key` (pinned in `device.json`). Everything later is verified against these two keys.
5. The platform log cannot be read before certification (`/v1/device/platform-log` needs an active device), so it is
   verified in reconcile step 2, before the device acts on anything (item 8).
6. Order: code parsed and config checked before any key exists; then `keys/device.key`, `POST /v1/device/enroll`
   (signed by the new key, answer verified with the pinned hub key, must be exactly `{"state":"pending_certification"}`),
   `device.json` last; any failure after the key file removes both. An existing identity is `already_enrolled`, a half
   one `identity_incomplete` (removed by `agent-runner uninstall`). Hub refusals and transport failures are reported
   by their closed code (e.g. `invite_unknown`, `response_unsigned`); the code is never printed.
7. Body: device ID UUIDv7, `os` = `darwin`, `arch` from the host (`[a-z0-9_]`, else `unknown`), `agent_version` =
   package version, `capacity` computed like the heartbeat with no VMs yet (physical memory − reserve).
   Without `--platform` the command calls `agent_runner.enroll.org.enroll(config, code, out)` (RUN-06); until that
   module exists it refuses with `unsupported enroll_org`.

## Verification before acting
8. The device acts only while the verified platform log certifies **its own keys** under its device ID and no
   `device_decertify` ended that certification. Otherwise the run halts after step 2 (`uncertified`,
   `decertified`, or `stopped` for a platform-log rollback/fork): no org log, no wrap, no pull.
9. Founder pinning of platform devices (ADR-040): the genesis key of the first verified copy is pinned, but the copy
   is accepted only when it makes this device a member through its own certification, the verified platform log gives
   the org platform access, and no workgroup, org or principal ID clashes with another verified copy; otherwise
   nothing is stored and `device_not_assigned` (or the entry's code) is audited. Afterwards L9 protects the copy.
10. An org is *acting* while its copy is verified and not stopped, the device is certified and is an active member
    through its current certification (a re-certification with new keys does not revive memberships of the old one).
11. Rollback: offered head (`state.orgs[].log_head`) or answered head below the verified head. Fork: same `seq` with
    another hash, or a refused entry with `seq` = head + 1 whose `prev` is not the verified head, or (inbox, L10) an
    `lh` the refreshed log does not reach. Each stops that org only (platform log: every org), persisted in
    `sync_state` as `org_stopped_<org hex>` / `platform_stopped`, so the alert is raised once and the org stays stopped
    across restarts until an operator clears the key (RUN-06 `doctor`).
12. Alert = log line `runner.alert <code> …`, audit items `runner.alert` (`{alert, value:null}`, result `failed`) and
    `runner.log_rollback` / `runner.log_fork` (`{verified_seq, offered_seq}`, result `rejected`), target `org`/ID or
    `platform_log`/`platform`, counter `log_alerts`.
13. Any other refused entry: the verified prefix is stored, the refusal audited (`runner.inbound_rejected`, source
    `log`/`platform_log`) once per daemon for the same org, entry position and code; the org keeps acting on its
    verified state.
14. Stored copies are verified again when loaded (fail closed: a copy that no longer verifies, e.g. after a change of
    the root key, fails the step instead of being trusted or silently refetched).

## Wraps (05 §2.2 step 3, 04 §6.3)
15. A wrap is used only for an acting org; a wrap of an unknown or stopped scope waits (not refused). `cred` wraps are
    checked against all acting orgs' logs; the device cannot learn the owner's current `CK` epoch from the logs, so the
    epoch bound of W1/W2 is the wrap's own epoch (RUN-09 may tighten it from the credential record's epoch). Refused
    wraps are audited once per fingerprint per daemon. A stored key wakes the outbox push (`seal_waiting`).

## Inbox (05 §2.3, §2.4)
16. Scopes pulled: the workgroups the device is assigned to in acting orgs plus their org scopes. Items are handled
    in `(scope, scope_seq)` order; gaps are normal (no `session.terminal` for devices); repeats at or below the
    cursor are ignored; an item for a scope that was not asked for is logged and ignored. A pull ends when the hub
    answers `more:false`, when nothing advanced, or after 1000 rounds.
17. Before an L10 refresh the header is checked (E1/E2), so a malformed event never refreshes or stops anything. The
    refresh covers the platform log first (a new `device_assign` may name a newer certification). An `lh` that the
    hub's own head reaches but whose entries did not verify refuses the event (`log_chain`) without stopping the org;
    only a hub that withholds the entries the author saw is a `log_fork`.
17a. Refused events (format, scope/org mismatch, E1–E5 incl. the author's right at `lh`, decertified authors, body not
    opening or not an object) are audited, counted and their cursor advances (a refused event never blocks a scope).
18. Kinds without a consumer are verified but not decrypted. A missing epoch key triggers one wrap refresh per pull;
    still missing, the scope waits (`key_waiting`, retried at the next pull) when the verified log says the device held
    that epoch, else the event is skipped with `runner.inbox key_missing` (an epoch before the device's assignment;
    not audited, it is no forgery). A waiting scope is left out of the following rounds of the same pull, so it never
    starves the others.
18a. Replays: `scope_seq` is not signed, so a hub can serve an old event again under a new `scope_seq`. The inbox
    refuses an event ID it handled in this daemon (`event_conflict`, bounded memory of 100 000 IDs) and ADR-039 makes
    consumers key their effects on the signed `event_id` (no schema change: prompts already have
    `prompt_delivery.event_id`), which also covers replays across restarts.
19. Settings and profiles have no consumer in this package: the registration point is ADR-039; their consumers come
    with the packages that use them (operations, harness context). The `prompt` hint and step 6 belong to RUN-05.

## Store
20. Pooled connections are not autocommit, so a later `transaction()` block would only be a savepoint committed (or
    rolled back) when the connection returns to the pool, leaving the in-memory copies ahead of the store. The steps and
    the inbox therefore use `orgs.db.session(pool)` (autocommit; every `transaction()` block commits at its end).
21. `log_head` is updated with every stored org entry; the outbox sealer (RUN-02) takes `lh` from it.

## Review follow-ups
26. Independent review: three high findings fixed (ID clashes between copies, item 9/ADR-040; stale platform copy in
    the L10 refresh, item 17; founder pinning as ADR with the platform-access condition, ADR-040), five medium fixed
    (E1 before the refresh, item 17; replays, item 18a; held keys and starvation, item 18; tests for clashes, L10
    with a new certification, replays, held keys, starvation, a transport failure during enrollment, and the shared
    L1-L8/P1-P4 vectors replayed through the book (`orgs/test_vectors.py`; empty logs and foreign founder keys do not
    apply, ADR-040)). Low fixed: scopes added mid-page, half-loaded book (locals assigned at the end), bounded
    refusal memory. Low accepted: the inbox reads the book across awaits without its lock; a reconcile that stops an
    org in that window lets at most the event being processed through (its checks passed against the verified state).

## Other packages' files
22. `tests/platform/runner/sync/test_tasks.py::test_real_package_registrations` (RUN-02) asserted the exact real
    registry; RUN-03 registers tasks, hints and connect hooks, so the test now checks only that the sync area's own
    entries are present (the same relaxation RUN-02 applied to RUN-01's tests, decisions/RUN-02.md item 9).
23. The ADR index (`docs/platform/adr/README.md`) gets the line of ADR-039, as RUN-02 did for ADR-028.

## Tests
24. Hub fully mocked (repository rule): `tests/platform/runner/reconcile/rig.py` extends the sync suites' fake hub
    (signature gate, signed answers, outbox rules) with state, platform log, logs, wraps and pull; every answer and
    query is validated against `contracts/`. Logs, wraps and events are built and signed with the reference builders of
    the crypto copy, independently of the runner code under test.
25. E2E-05, E2E-06 and E2E-14 do not exist yet in agent-core (only `e2e-00-smoke`); they are run by their scenario
    packages (SCN-01/SCN-02/SCN-03).
