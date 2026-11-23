# HOT-04 - No false `wrap_unauthorized` audit for a wrap ahead of the verified log

## Cause

The admin's browser stores the `device_assign` of a workgroup and the device's key wrap in one request (one hub
transaction). A reconcile run requests `GET /v1/device/state` (step 1), refreshes the logs up to the heads offered
there (step 2) and fetches the wraps afterwards (step 3). When the browser's request lands between step 1 and step 3,
the wrap step holds a wrap whose entitlement the verified log does not show yet: W3 refuses it as
`wrap_unauthorized`. The hub dump of a red E2E-11 run shows the `device_assign` and the wrap with the same timestamp
and the device's `runner.inbound_rejected` (`source=wrap`) 76 ms later. E2E-14 expects no `runner.*` audit of the
device over the whole run and failed whenever this happened (roughly one run in four).

SCN-08 item 3 made the refusal hold only for the verified log state it was judged against (the wrap is checked again
once the logs advance), but audited it once per log state. SCN-08 item 4 then held the first such refusal back for
one run instead of judging it. Both left the judgement to the timing of the next run and against a log the device
knew to be possibly behind; item 3 also audited the same wrap again at every later log state.

## Correction (`src/agent_runner/reconcile/wraps.py`)

1. A `wrap_unauthorized` of a wrap addressed to this device (the one refusal a log state can change) is not judged
   against the verified copy as it is: the wrap step first refreshes the platform log and every acting org's log
   from the hub, exactly like the inbox's L10 refresh (`refresh_platform`, `refresh_org(..., None)` under the book
   lock, with all L1-L9 checks, rollback and fork alerts), once per run, and checks the wrap again.
2. Entitled by the refreshed logs: stored, no audit. Still refused: audited, once per wrap (`("audited", ...)` in the
   book's refusal memory). The refusal itself still holds only for the log state it was judged against, so the wrap
   is checked again when the logs advance (SCN-08 item 3 kept), but a repeated refusal is not audited again.
3. Each log is refreshed on its own and the wrap is checked again in any case, so a failing log of another org does
   not hold back a wrap its own refreshed log entitles. A refresh that failed for any log (transport error, refusal,
   unusable answer) and a wrap still refused: neither stored nor audited, tried again later. After `REFRESH_TRIES`
   (3) reconcile runs without a complete refresh it is judged against the verified copy alone (and audited if
   refused), so a hub that serves wraps but withholds the logs cannot keep a wrong wrap out of the audit for good.
   Only reconcile runs count (`sync_wraps(run, counting=True)` from step 3); the inbox's calls of the wrap step
   (a scope waiting for a key) never judge such a wrap, so several pulls during a short outage cannot use up the
   tries. A hub that cannot be reached at all fails step 1 already; the wrap step does not run and counts nothing.
4. No wrap is stored before its entitlement is in the verified log: the second check is the same `check()` on the
   refreshed copy. Every other refusal (other recipient, signature, decryption, malformed) is audited at once as
   before and not checked again.

SCN-08 items 3 (audit once per log state) and 4 (hold back once) are superseded by this; the recheck after the logs
advanced remains.

## Decisions

- The refresh covers all acting orgs, not only the wrap's org: `cred` wraps depend on every acting org, the refresh
  runs at most once per run and only when a wrap for this device was refused, and the number of orgs of a device is
  small. The platform log is refreshed too, since W3 excludes decertified platform devices.
- The refusal memory stays in memory (`OrgBook.refused`, as for every refused inbound item): after a runner restart a
  wrap is judged again, with a fresh refresh. A genuinely unentitled wrap is therefore audited once per daemon life,
  the same rule as all other refusals.
- The retry counter is kept as marker tuples in the same bounded refusal memory; when that memory is cleared (bound
  reached) a counter starts again, which only delays an audit.

## Review

Independent review (no high findings): the stale-offer test now asserts that no `runner.*` audit appears and the org
keeps acting, and no longer runs on with a lasting stale offer (that is a rollback, L9, by design); request counts
pin that a refused wrap does not refresh the logs again while they did not move; the inbox no longer counts towards
`REFRESH_TRIES`; logs are refreshed one by one. Not changed: the audit marker is set before the audit item is
written (the existing pattern of every refusal; a failing store write halts the run anyway); a `device_decertify`
found by the refresh leads to `wrap_unauthorized` audits instead of a halt in this step (the next run's step 2 halts;
same as the L10 refresh); the refusal memory is per daemon (see above).

## Tests

`tests/platform/runner/reconcile/test_wraps_ahead.py`: wrap stored right after the state answer (the race itself),
stale offered head, wrap after its log entry (no refresh), wrap without any entitlement (audited once, not again at a
later log state, stored once the log entitles it), wrap for a device removed from the workgroup (audited), logs
unavailable for the refresh (no audit, stored on the next run), refresh failing `REFRESH_TRIES` times (audited once),
runner restart between wrap and refresh (stored, no audit), another org's log failing (the wrap is still stored),
the inbox's calls never judging a wrap whose logs cannot be refreshed. Six of the first eight are red against the previous wrap step; the
log-before-wrap and removed-device tests guard behaviour that did not change. In `test_wraps.py` the expectations
that encoded "held back once, audited in the second run" now expect the audit in the first run and no second audit.

## Not changed

No contract change, no change to the E2E specs. `docs/platform/spec-deviations.md` (shared with agent-core) still
describes the row of SCN-08 item 3 (`§6.3 W3`), which remains true; the refresh before judging is an addition for the
documentation package.
