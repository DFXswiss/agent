# SCN-08 – decisions (agent part: outbox push latency for PERF-03)

1. **The batch is taken after the rate wait.** PERF-03 (runner outbox write → browser frame, p95 < 300 ms) measured
   p50 ≈ 400 ms on the runner device with the hub → browser part at p50 ≈ 7 ms: `Pusher.push_once` took the batch
   *before* waiting out the 250 ms interval (03-api.md section 1), so an item committed during that wait missed the
   batch and waited one more interval; with events every 150 ms most items were in that case (newest item of a batch:
   p50 ≈ 340 ms from outbox write to hub receipt). A batch that had to wait is now taken again after the wait; the
   interval itself (250 ms after the end of the previous push) is unchanged, so the device rate budget at the hub
   holds. A push with nothing due still returns at once without waiting. Tests:
   `test_items_written_during_the_wait_go_with_the_batch`, `test_an_event_read_again_after_the_wait_is_sealed_once`.
   The second read happens under the pusher's lock right after the first, so it cannot come back empty; when a
   backlog drains, a batch that waited is read (and decrypted) twice - accepted, the wait dominates.
2. **Ownership.** The runner's push loop and wrap reconcile are not owned by SCN-08; both changes are the small fixes
   the package allows for defects its scenarios find (PERF-03 and E2E-26), with tests, recorded here and in the board
   report. The agent-core part of the package is described in
   agent-core `docs/platform/decisions/SCN-08.md`.
3. **A refused wrap is checked again when the logs advance.** E2E-26 (mobile project, and any timing where it
   happens) hung for good: the admin's browser stores the `device_assign` of a new workgroup and the device's wrap in
   one request; the runner fetched the wrap (wraps hint) before it had verified that log entry, refused it as
   `wrap_unauthorized` and remembered the refusal for the daemon's life, so the device never got the workgroup key and
   the gapless outbox stopped at the first event of that workgroup (`seal_waiting`) - every later scenario of the run
   hung with it. The refusal key now contains the verified log heads the check depended on (the org's, all acting
   orgs' for `cred` wraps, and the platform log's) only for a `wrap_unauthorized` of a wrap addressed to this device
   (the one refusal the logs can change); it is audited once per such log state and checked again after the logs
   advanced. Every other refusal (other recipient, signature, decryption, malformed) stays audited once, so a hub
   serving junk wraps cannot make each log advance re-check and re-audit them (review finding). Test: `test_a_wrap_ahead_of_its_log_entry_is_checked_again_when_the_log_advances`.
4. **A wrap ahead of its log entry is held back once, not audited.** With item 3 the race still wrote one
   `runner.inbound_rejected` per new workgroup of a platform device - a false alarm in the audit (E2E-14 checks that a
   device writes none). The first `wrap_unauthorized` of a wrap addressed to this device is held back; the next
   reconcile run pulls the logs first and audits it if it refuses it again (held back once per wrap, whatever the log
   state, so a hub releasing log entries one by one cannot postpone the audit; review finding). Other
   refusals are audited at once as before. `test_first_prompt_orders.py` (SCN-06 order) now expects no audit line.
