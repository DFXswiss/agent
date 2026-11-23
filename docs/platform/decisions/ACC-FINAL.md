# Decisions of ACC-FINAL (final acceptance)

The same file exists in `agent-core` and `agent`. Evidence: `docs/platform/acceptance/FINAL.md`.

1. **The final matrix is the E2E scenarios; PERF counts only from exclusive runs.** As in ACC-M3 (decision 1): the
   four functional lanes (T1 and T2, desktop and mobile) run `--only E2E-00,…,E2E-39` on one pinned pair of
   integration states (`contracts/VERSION` equal); PERF-01…05 come from exclusive runs only (section "Performance"
   of `FINAL.md`).
2. **A new exclusive PERF run on the end state.** KOR-13's final measurement used agent-core `f92cf9ef` with agent
   `64388e2b`. KOR-18 merged afterwards changes the runner's hub client (idle connections reused for at most 2 s,
   one repeat of a request lost on a reused connection) and the hub's idle timeout (120 s) – both on the push path
   that PERF-03 measures. The rule of the package (performance-relevant product code merged after the final
   measurement ⇒ one run per topology without `--repeat`) therefore applies; the result replaces nothing in
   `performance/results.md` but is recorded in `FINAL.md` next to it.
3. **ATK re-run, LIVE not re-run.** The runner changed after the last ATK run (ACC-M3, agent `936e599a`) in the
   inbound request judgement (KOR-17), the VM pool and clone path (PFM-01, KOR-15) and the hub client (KOR-18); the
   attack suite therefore ran again on the end state (`scripts/run-atk.sh --redteam`). The live tests (LVT-01) were
   not re-run: the changes since then on their paths are a diagnostic field of the GitHub client (`cause`, no
   behaviour change), the retry of the first device-flow call (the live tests use no device flow) and the hub-client
   connection handling, which every hermetic E2E scenario of the final matrix exercises; a live run would also need
   the subscription credentials of a person.
4. **The traceability report follows the markers in both directions.** The check (`e2e/platform/orchestrator/
   traceability.py`) required that a requirement tag in a title lists the title's tests, but not that a spec test a
   requirement lists carries that requirement's tag. Ten pairs had no tag (R-6.3/E2E-21, R-9.7/E2E-23,
   R-11.1/E2E-22, R-14.2/E2E-10, R-14.5/E2E-22, R-14.6/E2E-14, R-14.7/E2E-21, R-P.5/E2E-25, -26, -33). The check now
   requires both directions and the tags are in the titles; the gate of `make platform-ci` runs for milestone 3
   (M3 is accepted, 153 of 153).
5. **R-3.12 is also proven by E2E-38.** The rescheduling case KOR-14 added shows the real `capacity_exhausted`
   refusal in the browser (`countdown.reason`); its title carries `@R-3.12 @R-7.8` and R-3.12 lists E2E-38
   (`01-requirements.md`, `10-work-packages.md` §5, `traceability.json`).
6. **R-2.1 asserts the prefill.** The independent sample review found that no platform E2E clicked a suggestion
   card; E2E-06 now clicks two chips and asserts the composer's text and focus (desktop and mobile green).
7. **Release-like web build in E2E (AUD-01 B-26).** `E2E_WEB_MINIFY=1` makes a run serve the minified production
   bundle (default stays unminified so that browser coverage maps onto the sources). The smoke E2E-00, E2E-01 and
   E2E-06 ran green against it. Compression at the edge is the deployment rule of `CUTOVER.md` ("Compression is the
   edge's job").
8. **Findings of the sample review kept as recorded deviations or residual risks, not changed here.**
   - R-4.4: the mailbox automation recognises the pages by the markers of the auth-page double; against the real
     authorize pages it falls back to the setup-token relay at the first step. The decision of R-4.4 is "hermetic
     only (no live login)"; the fallback keeps the login working. Adapting the page recognition needs a live login
     by a person (residual risk, register row added).
   - R-8.9: `/v1/stream` writes no `read.*` row (recorded deviation, 03 §1 read log); the read log is written after
     the response from a bounded queue (best effort; register row added).
   - R-11.6: the runner retries a retryable operation failure three times with the delays 2 s, 8 s, 30 s (four
     attempts) and ends at once with `retryable:true` when `Retry-After` exceeds 60 s; 03 §9 says "3 attempts,
     delays 2 s, 8 s, 30 s", which names three delays (register row added).
9. **Register checked and cleaned.** Every "Unverified" bullet of `spec-deviations.md` was checked against the code
   (31 bullets): 21 no deviation, 8 deviation (all low or harmless), 2 specification errors. Bullets settled in the
   code (IOAccounting, `invite.use`, `user_invite` takeover closed by ADR-043, read-log placement) or fixed here
   (quota polling, decision 10; the cross-reference 02 §5 → 03 §7.2; the Landlock wording of 05 §4.5) were removed,
   the result is appended to the others. Of the 19 rows marked "deviation", 16 were checked: 14 correct, the
   credential-record row is no longer a deviation (KOR-07 audits the refusal) and the code-splitting row was stale
   (security page lazy, budget 300 KiB, SRI on lazy chunks); both rows are corrected.
10. **Quota polling follows the hub's election (medium defect, fixed).** The hub names the online eligible device
   with the smallest ID in `quota_poll_owners`, but the runner polled only owners with a session on this device in
   the last 24 h and a cached record: when the owner's sessions ran on another device nobody polled and the quota
   view stayed empty. The runner now polls every owner the hub names and fetches a record that is not cached (at most
   once per 900 s) after refreshing the desired state and the logs, so that an owner ahead of the device's log copy
   (a user who just joined) is never refused and audited falsely (the rule of KOR-17; found by the review of this
   package); 05 §10.6 says so. A record that really fails to verify is audited again at most every 900 s while the
   hub names its owner. Unit tests `tests/platform/runner/quota/test_subscription.py` (agent); E2E-16 green.
11. **A lazily loaded part whose chunk cannot be fetched degrades to its fallback (medium defect, fixed).** In the
   final matrix E2E-14 failed on T1 mobile: while the browser's link to the hub was cut, the chat rendered its first
   output and requested the lazily loaded Markdown chunk; the request failed, the integrity-checking loader rejected
   the import ("integrity check failed") and the route's error boundary replaced the whole task screen with "Unexpected
   error" – after the link returned nothing recovered without a reload. A browser keeps a failed module as failed until
   the page reloads, so a retry of the same chunk cannot help. The in-page lazy parts (Markdown in the chat and on
   boards, the uPlot chart, the terminal view) are now wrapped in `ui/lazy/ChunkBoundary.tsx`, which renders their
   plain fallback (the text, the sparkline, "The terminal view could not be loaded … Reload the page.") instead;
   nothing of a refused chunk runs, so the integrity check stays fail-closed. The boundary catches only an unavailable
   chunk (the loader's typed refusal `ChunkUnavailable` or the browser's failed dynamic import); any other error of the
   loaded code still reaches the route's error page (finding of the review of this fix). Whole screens loaded lazily
   keep the error page with its reload button (`web/e2e/lazy-integrity.spec.ts`). Given up on purpose: for the in-page
   parts a refused (tampered) chunk no longer ends on the error page – the content shows unformatted; this supersedes
   the statements "the screen ends on the error page" of `decisions/KOR-04.md` and `decisions/KOR-11.md` for these
   parts (`web/e2e/devices/lazy-stylesheet.spec.ts` now expects the chart's sparkline). Tests: `web/tests/ui/chunk-boundary.test.tsx`,
   `web/e2e/devices/lazy-unavailable.spec.ts` (red before: the chat output vanished), E2E-14 in the repeated matrix.
12. **PERF-03 on T2 is fixed by a hedged outbox push, not left as a residual risk (agent, ADR-050).** The two
   exclusive end-state runs on T2 gave 303.2 ms and 287 ms against the bound of 300 ms; the hub's share was 8 ms, the
   tail came from single pushes hanging about half a second on the wireless link while every later item waited behind
   them (one push on the way, ADR-048). A requirement that misses its bound is a gap of this acceptance, so it is
   fixed here: a push still unanswered after three times the median answer time of the last 16 answered pushes (at
   least 150 ms) is sent once more with the same bytes on another connection, the first answer counts; at most one
   copy per 2 s (the hub's pacer never sees more than it refills) and none above 256 KiB. The hub needs no change: it
   serialises a device's pushes and answers stored items `replayed`. The independent review found no high finding;
   its medium findings (pacer drain by copies, copies of large batches, tests that bypassed the test default) are
   fixed in the second commit. Re-measured exclusively on the final pair (agent-core `5e2a9e88`, agent `23ef07dd`):
   PERF-03 T2 192.4 ms (link mean 68 ms, as before), T1 17.1 ms, during PERF-05 177.2 ms; PERF-01, -02, -04, -05 pass;
   the whole suite ran again on the final pair (matrix round 4). ATK and LIVE are not re-run: the change touches only
   the runner's outbox push, which neither suite checks beyond what the E2E matrix covers.
