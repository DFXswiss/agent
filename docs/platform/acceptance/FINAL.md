# Final acceptance (ACC-FINAL)

Package ACC-FINAL, 2026-11-23. The unconditional proof that `KONZEPT.md` is implemented (R-P.1): every requirement of
`docs/platform/spec/01-requirements.md` with its implementation and a passing test, all coverage gates at 100 %, the
full E2E suite on both topologies and both projects, the performance, security, live, zero-knowledge and recovery
evidence checked for currency, documentation, scans, R-P.11, draft PRs. The same file exists in `agent-core` and
`agent`. Devices are named by role (runner device, second device, workstation; see `M3.md`). Decisions:
`docs/platform/decisions/ACC-FINAL.md`.

**Result: accepted.** All 153 requirements are implemented and proven (table "Requirements"), all gates are green
with 100 % line and branch coverage, the E2E suite is green on T1 and T2, desktop and mobile (round 4 on the final
pair), PERF-01…05 hold their bounds in exclusive runs on the final pair – PERF-03 on T2 after the hedged outbox push
(decision 12, ADR-050) with 192 ms instead of 303 / 287 ms, on the same wireless link (section "Performance") –, the scans are clean, `develop` of both
repositories is untouched and the draft PRs are final. Open for a person: the manual rows of the PRF device matrix
and the other items of section "Residual risks and items for a person". Not part of this package: merge into
`develop`, deployment, cutover.

## Tested states

| Run | agent-core | agent | Golden image |
|---|---|---|---|
| Gates at the start (fresh checkouts of the integration heads after KOR-18) | `c634b3af` | `7686f936` | – |
| Matrix round 1 (four lanes) | `c634b3af` | `7686f936` | `cache-golden-42cc7cdff91c` |
| Functional lanes of the fixes (minified build, E2E-06, E2E-16/-27/-28) | ACC-FINAL branch | ACC-FINAL branch | `cache-golden-42cc7cdff91c` |
| Exclusive PERF runs | ACC-FINAL branch on `c634b3af` | ACC-FINAL branch on `7686f936` (quota change only) | `cache-golden-42cc7cdff91c` |
| ATK | – | `7686f936` | `cache-golden-42cc7cdff91c` |
| Matrix round 2 | `bd6c8b9f` | `dd400180` | `cache-golden-42cc7cdff91c` |
| Matrix round 3 | `88c53fc0` | `086361ee` | `cache-golden-42cc7cdff91c` |
| Pair after the lazy-chunk review (last merge gates, targeted lanes) | `ae9ef379` | `d1e05b44` | `cache-golden-42cc7cdff91c` |
| **Final pair** after the hedged push (decision 12): merge gates, exclusive PERF T1/T2, matrix round 4 | `5e2a9e88` | `23ef07dd` | `cache-golden-42cc7cdff91c` |

`contracts/VERSION` is identical in both repositories for every pair (`dd6eefc1…`); every lane pins the other
repository's commit (`WP_OTHER_REF`). The golden image of every agent state used here has the same content hash
(`tools/wp/golden-cache.sh … ensure` before each run): this package changed no guest or image code.

## Acceptance criteria

| Criterion (package text) | Evidence | Result |
|---|---|---|
| every one of the 153 requirements: implementing place and at least one green E2E test (R-14/R-P: the gate of `10-work-packages.md` §5) | section "Requirements" | met: 153 of 153 |
| sample of at least 20 requirements against the code by an independent reviewer | section "Independent checks" | met: 24 requirements, findings handled |
| all gates 100 % | section "Gates" | met |
| full E2E suite once on T1 and T2, desktop and mobile, green | section "Final matrix" | met |
| PERF-01…05 (exclusive) | section "Performance" | met on the final pair (PERF-03 T2 192 ms after decision 12; confirmed by KOR-19 with 230.7 / 246.8 ms) |
| ATK, PERF, LIVE, zero-knowledge, DR current | section "Currency of the evidence" | met (ATK and PERF re-run; LIVE unchanged paths, decision 3; the hedged push changes no path ATK or LIVE checks, decision 12) |
| documentation complete and consistent | section "Documentation" | met |
| scans clean (secrets, host names, IPs, accounts; files, history, messages; signatures) | section "Scans" | met |
| R-P.11 | section "R-P.11" | met |
| draft PRs final | DFXswiss/agent#234, DFXswiss/agent-core#57 | updated, draft, not merged |
| devices cleaned up | package report | done (VMs and golden caches of the project removed, never `golden`/`spikeb`) |

## Gates

Fresh checkouts of both integration heads, `tools/wp/gates.sh` (the steps of `make platform-ci`; the agent run
includes `make platform-ci-linux` in the Linux VM on the runner device and waits for its own Linux part):

| Repository | Result |
|---|---|
| agent-core | green: lint, types, contracts (CON-01…04); `pytest tests/platform` 10 501 passed, 1 skipped (the sibling-checkout test, run separately below), `agent_hub` 100 % lines and branches (13 691 statements, 2 898 branches); web: lint, types, 1 669 Vitest tests (`src/core` 100 %), 179 Playwright tests, merged web coverage 100 % lines and branches, build check (two builds byte-identical, SRI/CSP); `e2e/platform` 369 tests, 100 % (5 321 statements, 1 380 branches) |
| agent | green: 9 109 passed, 6 justified skips (sibling checkout; `limactl` version on the workstation; two vector cases by design; ATK selection), Linux part green (kernel 6.8, Landlock ABI 4); combined coverage of macOS and Linux 100 % lines and branches (33 941 statements, 8 238 branches) |
| shared documentation | `tests/platform/test_docs.py::test_shared_documentation_equals_the_other_repository` with `PLATFORM_SIBLING_CHECKOUT` naming a fresh export of the other integration head: passed in both repositories (not skipped) |
| traceability | `python e2e/platform/test_traceability.py --milestone 3 --agent <agent checkout>`: green, 153 of 153 due requirements; the gate of `make platform-ci` now checks milestone 3 and both directions of the markers (decision 4) |

Merge gates of the final pair (`tools/wp/merge.sh`): agent-core green – `agent_hub` 100 % (13 691 statements, 2 898
branches), web 1 673 Vitest tests and 180 Playwright tests with merged coverage 100 %, `e2e/platform` 372 tests 100 %
(5 333 statements, 1 390 branches); agent green – 9 113 passed, 6 skipped, Linux part green, combined coverage 100 %
(33 975 statements, 8 256 branches). Two merge gates were red once and fixed with a commit each (a race inside the
relay unit test; a browser test that pinned the error page replaced by decision 11).

## Final matrix

Every scenario of `07-testing.md` §2.1 in four lanes (`tools/wp/e2e-par.sh`, one isolated stack per lane), desktop
(`Desktop Chrome`) and mobile (`Pixel 7`, includes E2E-29), `--only E2E-00,…,E2E-39` (PERF only from exclusive
runs, decision 1); T1-only scenarios skip on T2 and E2E-32 runs only on T2. Every lane: retries 0, egress check of
both devices, `agent-hub audit verify` after the run, VMs of the run deleted, credential and plaintext scans.

Round 1 – integration heads at the start (agent-core `c634b3af`, agent `7686f936`):

| Combination | Lane | Result | Tests | Duration | Egress violations | E2E-09 exports |
|---|---|---|---|---|---|---|
| T2 desktop | t2-l3 | green | 27 passed, 15 skipped | 21.0 min | 0 | 11, 0 findings |
| T2 mobile | t2-l4 | green | 28 passed, 15 skipped | 21.0 min | 0 | 11, 0 findings |
| T1 desktop | t1-l1 | red: E2E-17 refused by the harness – the monitoring archives of the lane were missing (`fetch.sh` prerequisite of SCN-07 not run for this package; environment, not product); 40 other tests green | 40 passed, 1 failed, 1 skipped | 32.8 min | 0 | 13, 0 findings |
| T1 mobile | t1-l2 | red: the same E2E-17 prerequisite; 41 other tests green | 41 passed, 1 failed, 1 skipped | 33.2 min | 0 | 13, 0 findings |

Cause fixed by fetching the archives on the runner device (`deploy/monitoring/test-stack/fetch.sh`); the final
round below runs the whole suite again on the merged heads.

T2 log check (KOR-16/KOR-18 rule) of every T2 lane: `github_connect retry` 0, `github_connect failed` 0,
`push_failed reason=hub_unreachable` only on fresh connections (`reused=no`) and only inside E2E-14's deliberate
offline window (hub audit `device.disconnect`/`device.connect` 15 s apart right after the session started working):
9 per lane; no failed push on a reused connection.

Round 2 – merged heads after the first merge of this package (agent-core `bd6c8b9f`, agent `dd400180`):

| Combination | Lane | Result | Tests | Duration | Egress violations | E2E-09 exports |
|---|---|---|---|---|---|---|
| T1 desktop | t1-l1 | green | 41 passed, 1 skipped | 34.4 min | 0 | 13, 0 findings |
| T2 desktop | t2-l3 | green | 27 passed, 15 skipped | 21.1 min | 0 | 11, 0 findings |
| T2 mobile | t2-l4 | green | 28 passed, 15 skipped | 20.9 min | 0 | 11, 0 findings |
| T1 mobile | t1-l2 | **red**: E2E-14 – the lazily loaded Markdown chunk was requested while the browser link was cut; the screen failed with "Unexpected error" and did not recover (product defect, medium, fixed: decision 11) | 41 passed, 1 failed, 1 skipped | 36.9 min | 0 | 13, 0 findings |

T2 log check of round 2: as in round 1 (no `github_connect retry`/`failed`; 9 `push_failed reason=hub_unreachable`
per lane, all `reused=no`, all inside E2E-14's offline window).

Round 3 – the whole suite again after the fix (agent-core `88c53fc0`, agent `086361ee`), all four combinations:

| Combination | Lane | Result | Tests | Duration | Egress violations | E2E-09 exports |
|---|---|---|---|---|---|---|
| T1 desktop | t1-l1 | green | 41 passed, 1 skipped (E2E-32, T2 only) | 34.7 min | 0 | 13, 0 findings |
| T1 mobile | t1-l2 | green | 42 passed, 1 skipped (E2E-29 included) | 35.0 min | 0 | 13, 0 findings |
| T2 desktop | t2-l3 | green | 27 passed, 15 skipped (T1 only) | 21.1 min | 0 | 11, 0 findings |
| T2 mobile | t2-l4 | green | 28 passed, 15 skipped | 21.4 min | 0 | 11, 0 findings |

Final pair (agent-core `ae9ef379`, agent `d1e05b44`): the review of the fix narrowed the boundary to unavailable chunks
and gave the terminal its own message (decision 11); the scenarios that render the lazy parts – E2E-12 (terminal),
E2E-14 (offline chat), E2E-17 (charts), E2E-25 (boards) – ran green on all four combinations of that pair (lanes
t1-l5, t1-l6: 4 passed each; t2-l7, t2-l8: 3 passed, 1 skipped (E2E-17 is T1 only) each); the agent side differs from
round 3 only by documentation.

T2 log check of round 3 and the final lanes: no `github_connect retry`, no `github_connect failed`; `push_failed
reason=hub_unreachable` only on fresh connections (`reused=no`) inside E2E-14's offline window (9 per whole-suite
lane, 4 per final lane).

Round 4 – the whole suite on the **final pair** after the hedged outbox push (agent-core `5e2a9e88`, agent
`23ef07dd`, decision 12), all four combinations, run side by side:

| Combination | Lane | Result | Tests | Duration | Egress violations |
|---|---|---|---|---|---|
| T1 desktop | t1-l1 | green | 41 passed, 1 skipped (E2E-32, T2 only) | 35.0 min | 0 |
| T1 mobile | t1-l2 | green | 42 passed, 1 skipped | 35.9 min | 0 |
| T2 desktop | t2-l3 | green | 27 passed, 15 skipped (T1 only) | 21.3 min | 0 |
| T2 mobile | t2-l4 | green | 28 passed, 15 skipped | 21.9 min | 0 |

Log check of round 4: no `github_connect retry`, no `github_connect failed`; `push_failed reason=hub_unreachable` only
on fresh connections (`reused=no`), in the same scenarios and numbers as round 2 (9 per T2 lane, 13 / 9 per T1 lane:
the deliberate outages); `push_refused` only `401 key_revoked` of the revocation scenarios, no `429`; copies of the
hedged push: 39 / 45 per T2 lane, 4 / 2 per T1 lane.

## Performance

The binding evidence is the exclusive final measurement of KOR-13 (`docs/platform/performance/results.md`, agent-core
`f92cf9ef` with agent `64388e2b`); its figures match the raw data (p95 recomputed nearest-rank from the CSV files:
PERF-03 26.6 ms T1, 291.1 ms T2, 180.8 ms during PERF-05; PERF-04 99.7 / 306.6 ms; PERF-01 0.64 / 0.77 s; PERF-02
6.70 s; PERF-05 p95 14.9 s). KOR-18 changed the push path afterwards, so this package measured once more on the end
state (decision 2), each run exclusive (`tools/wp/e2e.sh … --exclusive`; all E2E and gate slots of the devices held,
no other run on them; default sample counts: 5 runs per time-to-working series, 1 000 events for PERF-03).

| Run (UTC) | Load before → after (1/5/15 min) | Running VMs before |
|---|---|---|
| T1 PERF-01…05, 07:10:11–07:27:54 | runner device 1.38 3.33 5.08 → 5.18 4.45 4.10 | 0 |
| T2 PERF-03, PERF-04, reconnect, run 1, 07:28:29–07:34:12 | runner device 3.45 4.10 3.98 → 2.65 2.59 3.25; second device 0.85 1.18 1.35 → 3.88 2.84 2.07 | 0 / 0 |
| T2 PERF-03, PERF-04, reconnect, run 2, 07:36:10–07:42:01 | runner device 1.31 2.06 2.95 → 1.55 1.68 2.45; second device 1.87 2.47 2.02 → 1.63 2.29 2.14 | 0 / 0 |

| Test | Bound | KOR-13 (binding) | ACC-FINAL end state | Result |
|---|---|---|---|---|
| PERF-01 new org / new workgroup, p95 | < 10 s | 0.64 / 0.77 s | 0.62 / 0.66 s | pass |
| PERF-02 clone path, p95 | < 10 s | 6.70 s (KOR-15: 6.57 s) | 6.58 s | pass |
| PERF-03 T1, p95 | < 300 ms | 26.6 ms | 15.5 ms | pass |
| PERF-03 during PERF-05, p95 | < 300 ms | 180.8 ms | 164.5 ms | pass |
| PERF-03 T2, p95 | < 300 ms | 291.1 ms | **303.2 ms** (run 1), 287 ms (run 2) | **at the bound** (finding below) |
| PERF-04 T1 / T2, p95 | < 1 s | 99.7 / 306.6 ms | 49.7 / 294.4 ms (run 2: 310.3 ms) | pass |
| Reconnect T1 / T2 (wait < 40 s, catch-up < 5 s) | | pass | runner wait p95 4.3 / 2.6 s, browser wait p95 17.7 / 16.2 s, catch-up ≤ 0.64 s | pass |
| PERF-05: 30 sessions working, 30 VMs, hub health, runner status | all | pass | 30/30 working in 30 VMs, p95 14.3 s, health p95 14 ms, status p95 247 ms, no 429 | pass |

**Finding PERF-03 on T2.** Decomposition of both T2 runs (method of KOR-13): hub receive → browser p95 8 ms in
both; runner outbox write → hub receive p95 297 ms (run 1) and 281 ms (run 2) at a median of 25 ms; the tail comes
in clusters where single pushes hang on the wireless link between the two devices (KOR-13 section 3.3). The link,
measured right before and after each run (120 pings each): mean 70 / 72 ms (run 1) and 68 / 76 ms (run 2), maximum
up to 217 ms, standard deviation 30–39 ms – against a mean of 26 ms when KOR-13 measured. The new code of KOR-18 did
not act during the runs: no request was repeated on a reused connection; the only failed pushes (12, fresh
connections) are the deliberate outages of the reconnect scenario. PERF-03 on T2 therefore holds its bound only
while the link is in a good state; on this wireless link the p95 sits at the bound (1 of 2 end-state runs 1 % over
it). Not a hub defect; the share of the platform is the runner's wait (≤ 100 ms by the leading-edge pacer) and 8 ms
at the hub. This finding is fixed by decision 12; the measurement on the final pair follows.

**Final pair after the hedged outbox push** (agent-core `5e2a9e88`, agent `23ef07dd`; decision 12, ADR-050), one
exclusive run per topology:

| Run (UTC) | Load before → after (1/5/15 min) | Running VMs before |
|---|---|---|
| T1 PERF-01…05, 10:03:28–10:20:58 | runner device 1.53 1.66 1.74 → 5.94 5.28 3.63 | 0 |
| T2 PERF-03, PERF-04, reconnect, 09:56:02–10:01:40 | runner device 1.57 1.37 1.72 → 1.90 1.76 1.79; second device 2.02 3.81 3.24 → 2.57 3.09 3.07 | 0 / 0 |

| Test | Bound | Final pair | Result |
|---|---|---|---|
| PERF-01 new org / new workgroup, p95 | < 10 s | 0.63 / 0.67 s | pass |
| PERF-02 clone path, p95 | < 10 s | 6.54 s | pass |
| PERF-03 T1, p95 | < 300 ms | 17.1 ms (1 133 events) | pass |
| PERF-03 during PERF-05, p95 | < 300 ms | 177.2 ms (1 140 events) | pass |
| PERF-03 T2, p95 | < 300 ms | **192.4 ms** (1 122 events, maximum 345.6 ms) | pass |
| PERF-04 T1 / T2, p95 | < 1 s | 54.9 / 290.7 ms | pass |
| Reconnect T1 / T2 (wait < 40 s, catch-up < 5 s) | | runner wait p95 2.6 / 2.2 s, browser wait p95 17.1 / 10.7 s, catch-up ≤ 11 ms | pass |
| PERF-05: 30 sessions working, 30 VMs, hub health, runner status | all | 30/30 working, p95 14.4 s, health p95 9.3 ms, status p95 230.7 ms, no 429 | pass |

Decomposition on T2 (method of KOR-13): runner outbox write → hub receive p95 185 ms (before 297 / 281 ms) at a median
of 25 ms, hub receive → browser p95 8 ms. The link right before and after the run (120 pings each): mean 68.2 / 69.3
ms, maximum 121 / 124 ms, standard deviation 31 / 30 ms – the same state as during the two earlier end-state runs.
The runner sent 54 copies on T2 (all after 150 ms) and 3 on T1 (during PERF-05), no `429`, no `push_refused`. The
p95 values were recomputed from the CSV files (nearest rank).

## After the acceptance (KOR-19)

The hedged push (decision 12, ADR-050) was the last product change. KOR-19 added the missing evidence for it,
without changing product behaviour (`docs/platform/decisions/KOR-19.md`).

- **New tests.** On the runner side, eight cases in `tests/platform/runner/sync/test_push_hedge.py`. They cover a
  refused first request with an answered copy and `reseal` under the hedge in both orders: a late request with the
  old bytes is refused with `409 origin_seq_conflict` and is never read by the runner (that it changes nothing at the
  real hub is shown by the hub tests below). They also
  cover the next push starting only after the given-up request has ended, a gap answered to both requests,
  realignment with a late request in the old numbering, a real conflict that stays fatal, and the hedge together
  with the KOR-18 repeat. On the hub side, four cases in `tests/platform/hub/outbox/test_late_copies.py`: a copy
  after later items is `replayed`, old bytes of a resealed item get `409` and nothing is written, a copy of rejected
  items gets no second audit, and a push and its copy are serialised by the row lock. Each new test was shown red
  against a fault built into the code (not committed); the table is in the decision.
- **ADR-050** §2 now states exactly what the hub answers a late request. The new §3 states the guarantee it rests on.
  "Consequences" records the accepted wait for the other request after a refusal.
- **Measurements.** PERF-03 on T2 after ADR-050, three exclusive runs: 192.4 ms (ACC-FINAL attempt 3), 230.7 ms and
  246.8 ms (KOR-19). The bound of 300 ms holds in all three, with 54 / 60 / 57 copies and no `429`.
  Details: `docs/platform/performance/results.md` (agent-core) section 8 and the decision.
- **States.** The whole E2E suite last ran on agent-core `5e2a9e88` / agent `23ef07dd` (round 4, section "Final
  matrix"). The later commits up to the KOR-19 merges (agent-core `93f3a346`, agent `7fa866e3`, then the KOR-19
  commits) contain only tests, comments, docstrings and documentation, so no E2E run was repeated. The end states are
  the heads of the integration branches after the KOR-19 merges, named in the draft pull requests.

## Currency of the evidence

| Evidence | Last run before | Changed paths since | Action | Result |
|---|---|---|---|---|
| ATK (ATS-03) | ACC-M3, agent `936e599a` | runner inbound judgement (KOR-17), VM pool and clone (PFM-01, KOR-15), hub client (KOR-18) | re-run `scripts/run-atk.sh --golden cache-golden-42cc7cdff91c --redteam` on agent `7686f936` | 105 passed, 1 skipped (the live red team needs a key on stdin), 82 s |
| PERF (PFM-01, KOR-13, KOR-15) | KOR-13, agent-core `f92cf9ef`, agent `64388e2b` | push path of the runner and the hub's idle timeout (KOR-18); hedged push (decision 12) | one exclusive run per topology (decision 2), again on the final pair (decision 12) | section "Performance" |
| LIVE (LVT-01) | agent-core `98fa3ab8` | GitHub client diagnostics, retry of the first device-flow call, hub-client connections | not re-run (decision 3) | LIVE-01…03 green (LVT-01); LIVE-04 not run – optional, not needed (no Anthropic API key by the owner's decision; R-4.3 is proven by E2E-27, the Claude live access by LIVE-03, subscription, read only) |
| zero-knowledge (SCN-09) | – | E2E-09 is part of the matrix | every lane | 0 findings (canary and plaintext scan including the organization exports: 13 on T1, 11 on T2) |
| DR (DR-01) | – | E2E-33 is part of the matrix | every lane | green |

## Requirements

Implementation places name the definition that realises the requirement's decision (paths at the start state of this
package; the quota poller and the traceability check at the merged state). Evidence: "final matrix" = the green
lanes of section "Final matrix" (E2E-32 runs only on T2); "exclusive runs" = section "Performance"; "LVT-01" =
`live.md`; "ATK on the end state" = section "Currency of the evidence"; "gates" = section "Gates"; "R-P.11 check" =
section "R-P.11"; "this document" = this acceptance. Three independent mapping passes found the places; a fourth,
independent reviewer checked a sample of 24 against the code (section "Independent checks").

| ID | Requirement | Implementation | Proving tests (final evidence) | Result |
|---|---|---|---|---|
| R-0.1 | Invitation-based, encrypted platform for AI-supported tasks. | agent-core:web/src/core/account/register.ts:77 register; agent-core:web/src/core/crypto/event.ts:99 sealEvent; agent-core:src/agent_hub/events/store.py:339 append_events | E2E-01 (final matrix), E2E-09 (final matrix) | proven |
| R-0.2 | Hierarchy consists exclusively of Organization → Workgroup → Task. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:320 hub.task (org 167, wg 214); agent-core:tests/platform/hub/test_schema.py:521 test_no_project_level_and_no_pull_request_table | E2E-08 (final matrix), U-HUB-schema (gates) | proven |
| R-0.3 | `agent-core` coordinates and synchronises. | agent-core:src/agent_hub/tasks/service.py:132 create_task; agent-core:src/agent_hub/scheduler/core.py:138 choose; agent-core:src/agent_hub/stream/publish.py:159 publish | E2E-06 (final matrix), E2E-38 (final matrix) | proven |
| R-0.4 | AI sessions run on assigned execution devices. | agent-core:src/agent_hub/scheduler/load.py:54 candidates (wg_device assigned, active, org member) | E2E-06 (final matrix), E2E-38 (final matrix) | proven |
| R-0.5 | Entry without configuration. | agent-core:web/src/ui/screens/invite/InviteScreen.tsx:25 InviteScreen (invite -> passkey -> private genesis); agent-core:web/src/core/tasks/send.ts:268 ensureDevice (auto-assign platform candidate) | E2E-01 (final matrix), E2E-05 (final matrix) | proven |
| R-1.1 | New users are added only by invitation. | agent-core:src/agent_hub/auth/register.py:131 begin (invite_id or authenticated add); agent-core:src/agent_hub/auth/register.py:146 begin_invited (invite_unknown) | E2E-01 (final matrix), E2E-02 (final matrix), U-HUB-passkeys (gates) | proven |
| R-1.2 | New customers are added only by invitation. | agent-core:src/agent_hub/platform/invites.py:63 create (require_inviter :41); agent-core:src/agent_hub/orgs/genesis.py:93 _consume_platform_invite; agent-core:web/src/core/platform/invites.ts:49 preparePlatformInvite | E2E-01 (final matrix) | proven |
| R-1.3 | New devices are added only by invitation. | agent-core:web/src/core/org/invites.ts:158 createInvite (kind device); agent-core:src/agent_hub/platform/cli.py:322 invite (operator invite-device); agent-core:src/agent_hub/device/enroll.py:119 _invite | E2E-04 (final matrix), E2E-05 (final matrix) | proven |
| R-1.4 | Invitations are generated as codes and passed on. | agent-core:web/src/core/crypto/invite.ts:53 formatCode; agent-core:web/src/core/org/invites.ts:151 link /invite#<code>; agent-core:src/agent_hub/crypto/invite.py:118 invite_id | E2E-02 (final matrix), E2E-03 (final matrix) | proven |
| R-1.5 | Users accept the invitation. | agent-core:web/src/core/account/join.ts:326 prepareAccept (sig + inv_sig); agent-core:src/agent_hub/log/verify.py:83 _accept | E2E-02 (final matrix) | proven |
| R-1.6 | Users set up a passkey. | agent-core:src/agent_hub/auth/ceremonies.py:72 creation_options (UV required, PRF); agent-core:web/src/core/account/register.ts:77 register (mandatory recovery code); agent-core:web/src/ui/screens/invite/RecoveryCodeStep.tsx:47 RecoveryCodeStep | E2E-01 (final matrix), E2E-30 (final matrix) | proven |
| R-1.7 | Users sign in at `agent.dfx.swiss`. | agent-core:src/agent_hub/auth/login.py:88 finish (session certificate <=12 h); agent-core:src/agent_hub/auth/ceremonies.py:57 relying_party (RP ID = host of AGENT_HUB_PUBLIC_URL); agent-core:web/src/core/account/login.ts:60 signIn | E2E-01 (final matrix), E2E-30 (final matrix) | proven |
| R-1.8 | Devices are paired with an invitation code. | agent:src/agent_runner/enroll/cli.py:32 enroll (--code); agent:src/agent_runner/enroll/org.py:510 run_enroll; agent-core:src/agent_hub/device/routes.py:104 post_enroll | E2E-04 (final matrix) | proven |
| R-1.9 | Devices then authenticate cryptographically with a device-specific key, like SSH, without requiring SSH. | agent:src/agent_runner/sync/http.py:227 Signer (agent-request-v1, Ed25519); agent:src/agent_runner/sync/ws.py:79 WsClient (signed upgrade); agent-core:src/agent_hub/auth/gate.py:150 verify_request | E2E-04 (final matrix), U-HUB-auth (gates) | proven |
| R-1.10 | One-time codes. | agent-core:src/agent_hub/orgs/append.py:172 _accepts (marks used); agent-core:src/agent_hub/orgs/genesis.py:93 _consume_platform_invite; agent-core:src/agent_hub/auth/register.py:77 OPEN_INVITE | E2E-03 (final matrix) | proven |
| R-1.11 | Expiry times. | agent-core:web/src/core/org/invites.ts:32 USER_EXPIRY_HOURS=72 / :33 DEVICE_EXPIRY_HOURS=24; agent-core:src/agent_hub/invites/orgs.py:126 create (max 30 d check :153); agent-core:src/agent_hub/config.py:81 invite_*_seconds limits | E2E-03 (final matrix) | proven |
| R-1.12 | Revocation / unpairing. | agent-core:src/agent_hub/invites/orgs.py:172 revoke; agent-core:web/src/core/org/devices.ts:297 unassignPlan / :307 unpairPlan; agent-core:src/agent_hub/platform/projection.py:154 mark_rotation_pending; agent-core:web/src/core/log/rotation.ts:327 decertificationBatch | E2E-03 (final matrix), E2E-04 (final matrix), E2E-05 (final matrix) | proven |
| R-1.13 | An existing user can accept an invitation to a further organization without a new identity. | agent-core:web/src/ui/screens/invite/org/OrgInvite.tsx:161 signInExisting ("I already have an account"); agent-core:web/src/core/account/join.ts:438 joinOrg | E2E-39 (final matrix) | proven |
| R-2.1 | Right after passkey login a first task is proposed (e.g. research or GitHub PR). | agent-core:web/src/ui/composer/Composer.tsx:21 CHIPS / :35 Composer (chip fills composer); agent-core:web/src/ui/screens/home/HomeScreen.tsx:17 HomeScreen | E2E-01 (final matrix), E2E-06 (final matrix) | proven |
| R-2.2 | An organization named in the invitation becomes the default context. | agent-core:web/src/ui/screens/invite/org/OrgInvite.tsx:201 (navigate to /o/<invited org>); agent-core:web/src/core/account/join.ts:438 joinOrg | E2E-02 (final matrix) | proven |
| R-2.3 | Otherwise a private organization is created automatically. | agent-core:web/src/core/account/genesis.ts:72 preparePrivateOrg (genesis private:true); agent-core:src/agent_hub/orgs/genesis.py:133 create_org | E2E-01 (final matrix) | proven |
| R-2.4 | Every organization has workgroups. | agent-core:src/agent_hub/log/verify.py:56 _genesis (default_wg) / :173 _wg_create; agent-core:web/src/core/org/workgroups.ts:118 createWorkgroupPlan | E2E-37 (final matrix) | proven |
| R-2.5 | Without explicit selection the default workgroup is used. | agent-core:web/src/core/tasks/send.ts:90 writableWorkgroups (default wg first); agent-core:web/src/ui/screens/home/HomeScreen.tsx:31 wg preselect | E2E-37 (final matrix), E2E-06 (final matrix) | proven |
| R-2.6 | Every user request, even a simple knowledge question, automatically creates a task. | agent-core:src/agent_hub/tasks/routes.py:70 create_task (POST /v1/tasks); agent-core:src/agent_hub/tasks/service.py:220 add_prompt (follow-ups); agent-core:web/src/core/tasks/send.ts:159 prepareTask | E2E-08 (final matrix) | proven |
| R-2.7 | The task has a unique ID. | agent-core:web/src/core/tasks/send.ts:167 uuid7 task id; agent-core:src/agent_hub/tasks/service.py:132 create_task (id_conflict); agent-core:src/agent_hub/db/migrations/0001_hub.sql:321 task.id PRIMARY KEY | E2E-08 (final matrix) | proven |
| R-2.8 | The task has a short description. | agent-core:web/src/core/tasks/title.ts:15 deriveTitle; agent:src/agent_runner/operations/executors.py:19 set_title | E2E-08 (final matrix) | proven |
| R-2.9 | The task is assigned to a workgroup. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:323 task.wg_id NOT NULL; agent-core:src/agent_hub/tasks/service.py:132 create_task | E2E-08 (final matrix), E2E-37 (final matrix) | proven |
| R-2.10 | Hierarchy Organization → Workgroup → Task; no separate project level. | agent-core:tests/platform/hub/test_schema.py:521 test_no_project_level_and_no_pull_request_table; agent-core:src/agent_hub/db/migrations/0001_hub.sql:320 hub.task | E2E-08 (final matrix), U-HUB-schema (gates) | proven |
| R-3.1 | AI works on tasks on physical execution devices. | agent:src/agent_runner/sessions/start.py:127 Starter; agent:src/agent_runner/vm/lifecycle.py:122 VmManager.ensure_vm | E2E-06 (final matrix) | proven |
| R-3.2 | Devices can be added to an organization. | agent-core:web/src/core/org/invites.ts:158 createInvite (device); agent:src/agent_runner/enroll/org.py:510 run_enroll; agent-core:src/agent_hub/device/enroll.py:231 enroll_org | E2E-04 (final matrix) | proven |
| R-3.3 | Devices can be assigned to workgroups. | agent-core:web/src/core/org/devices.ts:280 assignPlan / :297 unassignPlan; agent-core:src/agent_hub/log/verify.py:235 _device_assign / :252 _device_unassign | E2E-38 (final matrix) | proven |
| R-3.4 | By default DFX provides its own execution servers. | agent-core:src/agent_hub/platform/cli.py:361 certify_pending; agent-core:src/agent_hub/device/enroll.py:268 enroll_platform; agent-core:web/src/core/tasks/send.ts:268 ensureDevice (auto-assign for private org) | E2E-05 (final matrix) | proven |
| R-3.5 | Users can optionally attach their own hardware. | agent-core:src/agent_hub/device/enroll.py:231 enroll_org (kind org); agent:src/agent_runner/enroll/org.py:510 run_enroll | E2E-04 (final matrix), E2E-36 (final matrix) | proven |
| R-3.6 | Initially only macOS is supported. | agent:src/agent_runner/config.py:133 check_platform (unsupported_os, called at :153) | E2E-04 (final matrix), U-RUN-config (gates) | proven |
| R-3.7 | Execution in Lima VMs: one separate VM per workgroup on an execution device. | agent:src/agent_runner/vm/lifecycle.py:122 VmManager.ensure_vm (per-wg lock); agent:src/agent_runner/store/migrations/0001_runner.sql:86 vm_wg_id_live unique index | E2E-11 (final matrix) | proven |
| R-3.8 | Several tasks of the same workgroup may share the VM. | agent:src/agent_runner/vm/lifecycle.py:122 VmManager.ensure_vm (reuses running VM of wg); agent:src/agent_guest/sessions.py:277 SessionManager | E2E-10 (final matrix) | proven |
| R-3.9 | … but must run in mutually isolated AI-session sandboxes. | agent:src/agent_guest/sessions.py:233 free_uid; agent:src/agent_guest/sandbox/unit.py:172 properties / :278 systemd_run; agent:src/agent_guest/sandbox/landlock.py:406 apply; agent:src/agent_guest/sandbox/settings.py:142 build_settings | E2E-10 (final matrix), ATK-01 (ATK on the end state), ATK-02 (ATK on the end state), ATK-03 (ATK on the end state), ATK-04 (ATK on the end state), ATK-05 (ATK on the end state), ATK-06 (ATK on the end state), ATK-07 (ATK on the end state), ATK-08 (ATK on the end state), ATK-09 (ATK on the end state), ATK-10 (ATK on the end state), ATK-11 (ATK on the end state), ATK-12 (ATK on the end state), ATK-13 (ATK on the end state), ATK-14 (ATK on the end state), ATK-15 (ATK on the end state), ATK-16 (ATK on the end state), ATK-17 (ATK on the end state), ATK-18 (ATK on the end state), ATK-19 (ATK on the end state), ATK-20 (ATK on the end state), ATK-21 (ATK on the end state), ATK-22 (ATK on the end state), ATK-23 (ATK on the end state), ATK-24 (ATK on the end state), ATK-25 (ATK on the end state), ATK-26 (ATK on the end state), ATK-27 (ATK on the end state), ATK-28 (ATK on the end state), ATK-29 (ATK on the end state), ATK-30 (ATK on the end state), ATK-31 (ATK on the end state), ATK-32 (ATK on the end state), ATK-33 (ATK on the end state), ATK-34 (ATK on the end state), ATK-35 (ATK on the end state), ATK-36 (ATK on the end state), ATK-37 (ATK on the end state), ATK-38 (ATK on the end state), ATK-39 (ATK on the end state), ATK-40 (ATK on the end state) | proven |
| R-3.10 | First task in a new org/workgroup: VM and session ready in < 10 s. | agent:src/agent_runner/vm/pool.py:420 Pool (warm pool); agent:src/agent_runner/vm/golden.py:399 Builder; agent-core:src/agent_hub/sessions/transitions.py:123 record (first_working_at) | E2E-06 (final matrix), PERF-01 (exclusive runs), PERF-02 (exclusive runs) | proven |
| R-3.11 | UI shows a countdown instead of a spinner. | agent-core:web/src/core/tasks/countdown.ts:121 countdown (from estimate_ms); agent-core:web/src/ui/countdown/CountdownPanel.tsx:122 CountdownPanel | E2E-06 (final matrix) | proven |
| R-3.12 | On delays the actual status stays visible. | agent-core:web/src/core/tasks/countdown.ts:121 countdown (WAITING_REASONS :38, step status); agent-core:src/agent_hub/scheduler/core.py:138 choose (device_offline/capacity_exhausted); agent-core:src/agent_hub/sessions/derive.py:65 task_state | E2E-07 (final matrix), E2E-38 (final matrix) | proven |
| R-3.13 | Initial server allocation: one DFX server hosts the hub, another hosts DFX cloud sessions. | agent-core:e2e/platform/orchestrator/run.py:173 Run (T1/T2 hub device + runner device); agent-core:e2e/platform/orchestrator/config.py:36 TOPOLOGIES | E2E-32 (final matrix T2) | proven |
| R-4.1 | Version 1 uses Claude Code as the only agent software. | agent:images/versions.json:2 claude_code pin; agent:src/agent_guest/sandbox/settings.py:120 DISABLE_AUTOUPDATER; agent:src/agent_guest/sandbox/unit.py:58 DISABLE_AUTOUPDATER | E2E-06 (final matrix) | proven |
| R-4.2 | Users can deposit their own Claude subscription credentials. | agent-core:web/src/core/credentials/records.ts:358 putCredential (sealRecord :219); agent-core:src/agent_hub/credentials/routes.py:103 PUT /v1/me/credentials/{kind}; agent:src/agent_guest/gateway_credentials.py:97 Credential.upstream_headers (subscription Bearer) | E2E-27 (final matrix), E2E-28 (final matrix), LIVE-03 (LVT-01) | proven |
| R-4.3 | … or an Anthropic API key. | agent-core:web/src/core/credentials/records.ts:358 putCredential (anthropic_api_key); agent:src/agent_runner/model/selection.py:113 credential_strategy; agent:src/agent_guest/gateway_credentials.py:97 Credential.upstream_headers (x-api-key injection) | E2E-27 (final matrix), LIVE-04 (not run – optional, not needed) | proven |
| R-4.4 | Reuse the existing e-mail login automation of `DFXswiss/agent` for Claude authentication. | agent:src/agent_runner/login/automation.py:141 magic_link (IMAP via agent_cli newest_magic_link) / :167 authorize (headless browser); agent:src/agent_runner/login/relay.py:128 LoginSession (setup-token relay); agent:src/agent_runner/config.py:169 LOGIN_AUTOMATION default by device kind | E2E-28 (final matrix) | proven; recorded deviation: page recognition only for the auth-page double, production… |
| R-4.5 | Without own credentials and own hardware: DFX servers and DFX model access via OpenRouter. | agent:src/agent_runner/model/selection.py:104 platform_strategy (platform devices only); agent:src/agent_runner/config.py:166 OPENROUTER_KEY_FILE refused on org devices; agent:src/agent_runner/model/provision.py:99 platform_credential | E2E-27 (final matrix), E2E-36 (final matrix), LIVE-01 (LVT-01) | proven |
| R-4.6 | Initial default model: DeepSeek. | agent:src/agent_runner/model/model-catalog.json:5 platform default deepseek/deepseek-v4.1-flash-1017; agent:src/agent_runner/model/selection.py:87 choose_model | E2E-27 (final matrix), LIVE-01 (LVT-01) | proven |
| R-4.7 | Check compatibility of Claude Code with the chosen DeepSeek model via OpenRouter. | agent-core:e2e/platform/live/live-01-openrouter.spec.ts (LIVE-01 full chain); agent:src/agent_runner/model/selection.py:93 platform_env | LIVE-01 (LVT-01), E2E-27 (final matrix) | proven |
| R-4.8 | Later support of other agent software is optional. | agent:src/agent_guest/driver.py:55 AgentDriver / :133 driver_for; agent-core:src/agent_hub/db/migrations/0001_hub.sql:366 session.agent_kind DEFAULT 'claude-code' | E2E-06 (final matrix) | proven |
| R-5.1 | Confidential data (prompts, answers, whiteboards, credentials) are encrypted client-side before transfer to t… | agent-core:web/src/core/crypto/event.ts:99 sealEvent; agent-core:web/src/core/credentials/records.ts:219 sealRecord; agent:src/agent_runner/sync/seal.py:141 StoreSealer | E2E-09 (final matrix), CON-03 (gates) | proven |
| R-5.2 | The hub must not see confidential content in plaintext. | agent-core:src/agent_hub/events/store.py:339 append_events (stores ct only); agent-core:e2e/platform/orchestrator/routes/canary.py:170 post_scan (canary scan of hub DB and logs) | E2E-09 (final matrix) | proven |
| R-5.3 | The hub manages only necessary metadata and encrypted content. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:282 hub.event (metadata + ct); agent-core:tests/platform/hub/test_schema.py:514 test_tables_and_columns_equal_the_specification / :531 test_no_column_names_confidential_plaintext | E2E-09 (final matrix), U-HUB-schema (gates) | proven |
| R-5.4 | Decryption only by authorized users and authorized execution devices. | agent-core:src/agent_hub/wraps/rules.py:112 store_wraps / :145 wraps_for; agent-core:web/src/core/log/rotation.ts:205 buildBatch (rotation on removal); agent:src/agent_runner/reconcile/wraps.py:130 sync_wraps (W1-W3 check) | E2E-31 (final matrix), E2E-18 (final matrix) | proven |
| R-5.5 | On a DFX execution server the user trusts that server with plaintext; zero knowledge towards the hub only. | agent:src/agent_runner/reconcile/wraps.py:130 sync_wraps (unwrap WK/OK/CK); agent:src/agent_runner/orgs/keys.py:25 store_scope_key; agent:src/agent_runner/credentials/cache.py:185 fetch | E2E-05 (final matrix), E2E-09 (final matrix) | proven |
| R-5.6 | Passkey login does not by itself yield an encryption key; key derivation, sharing and device distribution mus… | agent-core:web/src/core/passkey/prf.ts:34 requirePrf; agent-core:web/src/core/crypto/bundle.ts:52 sealBundle; agent-core:web/src/core/crypto/recovery.ts:19 generateRecoveryCode; agent-core:web/src/core/crypto/wrap.ts:43 wrapKey (HPKE) | E2E-30 (final matrix), E2E-31 (final matrix), CON-02 (gates) | proven |
| R-6.1 | All relevant events are stored immediately locally on the execution device in its database. | agent:src/agent_guest/journal.py:176 Journal.append; agent:src/agent_runner/sync/outbox.py:215 enqueue | E2E-14 (final matrix) | proven |
| R-6.2 | … and synchronised encrypted to the hub with minimal delay. | agent:src/agent_runner/sync/tasks.py:31 listen; agent:src/agent_runner/sync/push.py:316 Pusher.push_once; agent-core:src/agent_hub/outbox/routes.py:33 post_outbox; agent-core:src/agent_hub/stream/publish.py:166 hint | E2E-14 (final matrix), PERF-03 (exclusive runs; T2 at the bound) | proven |
| R-6.3 | Covered: prompts, answers, tool events, results, status, errors, file changes. | agent:src/agent_guest/transcript.py:155 TranscriptParser; agent:src/agent_runner/sessions/journal.py:173 event_handler; agent-core:src/agent_hub/log/events.py:78 KIND_RULES | E2E-13 (final matrix), E2E-21 (final matrix) | proven |
| R-6.4 | Covered: token usage, costs, context-window utilisation. | agent:src/agent_guest/gateway.py:181 UsageMeter; agent:src/agent_runner/model/usage.py:103 usage_item; agent-core:src/agent_hub/usage/outbox.py:80 handle_usage; agent-core:web/src/ui/task-tabs/chat/usage-strip.tsx:14 UsageStrip | E2E-16 (final matrix) | proven |
| R-6.5 | Covered: Claude subscriptions and available quotas, as far as technically readable. | agent:src/agent_runner/quota/subscription.py:86 SubscriptionPoller; agent-core:src/agent_hub/usage/outbox.py:103 handle_quota; agent-core:web/src/ui/settings/sections/quotas/index.tsx:51 QuotasSection | E2E-16 (final matrix), LIVE-03 (LVT-01) | proven |
| R-6.6 | `agent.dfx.swiss` shows current state and time-precise events also on smartphones. | agent-core:web/src/ui/task-tabs/chat/EventTime.tsx:7 EventTime; agent-core:web/src/ui/responsive/BottomNav.tsx:67 BottomNav | E2E-29 (final matrix) | proven |
| R-6.7 | Every action, change and execution is logged with actor, time, task, session, workgroup, device, target, resu… | agent-core:src/agent_hub/audit/chain.py:206 append_audit; agent-core:src/agent_hub/events/store.py:250 _store | E2E-15 (final matrix) | proven |
| R-6.8 | Append-only event log with replay after connection interruptions. | agent-core:src/agent_hub/outbox/process.py:132 plan; agent-core:src/agent_hub/events/store.py:339 append_events; agent-core:src/agent_hub/stream/sse.py:201 replay_events; agent-core:web/src/core/net/cursors.ts:13 Cursors | E2E-14 (final matrix), E2E-33 (final matrix) | proven |
| R-6.9 | Absolutely delay-free network synchronisation cannot be guaranteed. | agent-core:src/agent_hub/stream/publish.py:120 LivePublisher; agent-core:src/agent_hub/stream/sse.py:293 BrowserStream | PERF-03 (exclusive runs; T2 at the bound), PERF-04 (exclusive runs), E2E-14 (final matrix) | proven |
| R-6.10 | Default view: chat with prompts and results. | agent-core:web/src/ui/task-tabs/chat/index.tsx:5 tab; agent-core:web/src/ui/task-tabs/chat/ChatTab.tsx:57 ChatTab | E2E-13 (final matrix) | proven |
| R-6.11 | Additional native Claude Code session view: full live terminal output and history. | agent:src/agent_guest/recorder.py:202 TerminalRecorder; agent-core:web/src/ui/terminal/TerminalView.tsx:92 TerminalView | E2E-12 (final matrix) | proven |
| R-6.12 | That view is read-only; prompts are entered only via the default view. | agent-core:web/src/ui/terminal/xterm.ts:39 openScreen; agent:src/agent_guest/prompts.py:91 PromptRegistry.check; agent:src/agent_guest/hooks.py:255 _user_prompt_submit | E2E-12 (final matrix), E2E-35 (final matrix) | proven |
| R-7.1 | Full resource monitoring on the levels device → Lima VM → AI session. | agent:src/agent_runner/metrics/outbox.py:88 samples_of; agent:src/agent_guest/metrics.py:200 vm_values; agent-core:src/agent_hub/outbox/handlers_metrics.py:50 handle | E2E-17 (final matrix) | proven |
| R-7.2 | CPU, RAM, storage, network, runtime, reachability, state and free capacity are visible. | agent:src/agent_runner/metrics/device.py:51 sample; agent:src/agent_runner/metrics/outbox.py:48 _sample; agent-core:src/agent_hub/monitoring/read.py:178 overview | E2E-17 (final matrix) | proven |
| R-7.3 | … and historised. | agent-core:src/agent_hub/monitoring/rollup.py:97 roll_up | E2E-17 (final matrix) | proven |
| R-7.4 | Reuse the monitoring stack of `DFXswiss/server`, in particular Grafana and proven components. | agent:src/agent_runner/metrics/textfile.py:89 write_atomic; agent:deploy/monitoring/dashboards/agent-platform-devices.json:1 | E2E-17 (final matrix) | proven |
| R-7.5 | Monitoring systems may run separately but must be technologically consistent. | agent:src/agent_runner/metrics/textfile.py:69 render; agent:deploy/monitoring/agent_monitoring/patch.py:155 render | E2E-17 (final matrix) | proven |
| R-7.6 | Relevant metrics are shown in `agent.dfx.swiss`. | agent-core:src/agent_hub/monitoring/routes.py:41 get_overview; agent-core:web/src/ui/screens/monitoring/MonitoringScreen.tsx:401 MonitoringScreen | E2E-17 (final matrix) | proven |
| R-7.7 | Bottleneck warnings. | agent-core:src/agent_hub/scheduler/core.py:67 Capacity.low; agent:deploy/monitoring/alerting/agent-platform.yaml:175 agent-runner-capacity-low | E2E-17 (final matrix) | proven |
| R-7.8 | Capacity-based task distribution. | agent-core:src/agent_hub/scheduler/core.py:101 Candidate.key; agent-core:src/agent_hub/scheduler/core.py:138 choose | E2E-38 (final matrix) | proven |
| R-8.1 | Persons can be added at organization level. | agent-core:src/agent_hub/invites/routes.py:66 create_invite; agent-core:web/src/ui/screens/members/InviteSection.tsx:269 InviteSection | E2E-02 (final matrix), E2E-18 (final matrix) | proven |
| R-8.2 | Persons can be added at workgroup level. | agent-core:web/src/core/org/members.ts:108 grantPlan; agent-core:src/agent_hub/log/verify.py:193 _grant; agent-core:src/agent_hub/log/verify.py:134 _invite | E2E-18 (final matrix) | proven |
| R-8.3 | Organization: member = read, admin = write; default for new members: read. | agent-core:src/agent_hub/orgs/rights.py:85 _org_right; agent-core:web/src/ui/screens/members/InviteSection.tsx:48 role default 'member' | E2E-02 (final matrix), E2E-18 (final matrix) | proven |
| R-8.4 | Workgroup: read or write; in the default workgroup all members have write by default. | agent-core:src/agent_hub/orgs/projection.py:38 effective_access; agent-core:src/agent_hub/log/verify.py:193 _grant; agent-core:web/src/core/org/members.ts:81 grantable | E2E-18 (final matrix), E2E-02 (final matrix) | proven |
| R-8.5 | Access rights enforced technically in the backend. | agent-core:src/agent_hub/orgs/rights.py:126 require; agent-core:src/agent_hub/orgs/rights.py:103 can | E2E-18 (final matrix), U-HUB-rights (gates) | proven |
| R-8.6 | … and in the user interface. | agent-core:web/src/core/log/state.ts:187 canWriteWg; agent-core:web/src/ui/task-tabs/chat/ChatTab.tsx:28 writer; agent-core:web/src/core/log/events.ts:147 hasRight | E2E-18 (final matrix) | proven |
| R-8.7 | Sessions may read and discover via platform APIs everything they are authorised for: members, workgroups, tas… | agent:src/agent_runner/queries/rights.py:59 read_scope; agent:src/agent_runner/queries/tools/list_members.py:38 list_members | E2E-19 (final matrix) | proven |
| R-8.8 | No blanket insight into foreign workgroups. | agent:src/agent_runner/queries/tools/list_workgroups.py:16 list_workgroups; agent:src/agent_runner/queries/rights.py:59 read_scope | E2E-19 (final matrix) | proven |
| R-8.9 | Read accesses are logged too. | agent-core:src/agent_hub/http/readlog.py:68 read_row; agent:src/agent_runner/subagents/journal.py:65 platform_query | E2E-15 (final matrix), E2E-19 (final matrix) | proven; recorded deviations: `/v1/stream` writes no read row; read log best effort (reg… |
| R-9.1 | Users can connect and disconnect their GitHub account. | agent:src/agent_runner/github/device_flow.py:224 connect; agent-core:src/agent_hub/credentials/routes.py:124 delete_credential; agent-core:web/src/core/github/connection.ts:155 disconnectGithub | E2E-20 (final matrix) | proven |
| R-9.2 | Authorised repository access. | agent:src/agent_runner/github/checkout.py:118 checkout; agent:src/agent_runner/github/pulls.py:107 get_pull_request; agent:src/agent_runner/github/repos.py:34 list_repositories | E2E-21 (final matrix), LIVE-02 (LVT-01) | proven |
| R-9.3 | Branches, commits, pushes, pull requests and comments. | agent:src/agent_runner/github/executors.py:11 EXECUTORS; agent:src/agent_runner/github/commit.py:243 commit; agent:src/agent_runner/github/push.py:76 push; agent:src/agent_runner/github/pulls.py:182 create_pull_request | E2E-21 (final matrix), LIVE-02 (LVT-01) | proven |
| R-9.4 | Rights limited to the connected account and released repositories. | agent:src/agent_runner/operations/checks.py:180 check (repo_not_allowed); agent:src/agent_runner/github/access.py:38 repository (repo_not_accessible) | E2E-22 (final matrix) | proven |
| R-9.5 | All actions are audited. | agent-core:src/agent_hub/operations/steps.py:52 audit_step; agent-core:src/agent_hub/outbox/handlers_operation.py:87 handle | E2E-15 (final matrix), E2E-21 (final matrix) | proven |
| R-9.6 | Check GitHub App with granular permissions. | agent:src/agent_runner/github/device_flow.py:82 start (GitHub App client ID); agent:src/agent_runner/github/ops.py:44 owner_client | E2E-20 (final matrix), LIVE-02 (LVT-01) | proven |
| R-9.7 | Claude Code must not run git commands itself; GitHub actions only as controlled platform operations. | agent:src/agent_guest/sandbox/settings.py:45 DENIED_TOOLS; agent:src/agent_guest/sandbox/unit.py:165 exec_allow_list; agent:src/agent_runner/operations/pipeline.py:473 Pipeline._execute | E2E-23 (final matrix), ATK-02 (ATK on the end state) | proven |
| R-10.1 | Every session automatically gets a standardised harness with org, workgroup, task and session context and doc… | agent:src/agent_runner/sessions/harness.py:86 session_context; agent:src/agent_guest/harness.py:121 render | E2E-23 (final matrix) | proven |
| R-10.2 | The harness states the prohibitions and the duty to request every action via `agent.dfx.swiss`. | agent:images/harness/RULES.md:9 What you must not do; agent:src/agent_guest/harness.py:88 load_rules | E2E-23 (final matrix) | proven |
| R-10.3 | Claude Code must not run programs, scripts, shell commands, git commands, tests or builds. | agent:src/agent_guest/sandbox/settings.py:142 build_settings; agent:src/agent_guest/sandbox/unit.py:165 exec_allow_list; agent:src/agent_guest/sandbox/landlock.py:247 plan | E2E-23 (final matrix), ATK-01 (ATK on the end state), ATK-02 (ATK on the end state), ATK-03 (ATK on the end state), ATK-04 (ATK on the end state), ATK-05 (ATK on the end state), ATK-06 (ATK on the end state), ATK-07 (ATK on the end state), ATK-08 (ATK on the end state), ATK-09 (ATK on the end state), ATK-10 (ATK on the end state), ATK-11 (ATK on the end state), ATK-12 (ATK on the end state) | proven |
| R-10.4 | Claude Code must not start subagents. | agent:src/agent_guest/sandbox/settings.py:45 DENIED_TOOLS (Agent/Task); agent:src/agent_guest/policy.py:242 Policy.evaluate | E2E-23 (final matrix), ATK-02/ATK-03 (Agent, Task; the reference to ATK-05, WebSearch, was corrected by KOR-40) (ATK on the end state) | proven |
| R-10.5 | Manipulating or circumventing hooks and security rules is forbidden. | agent:src/agent_guest/sandbox/settings.py:142 build_settings (allowManaged*Only, Edit deny rules); agent:src/agent_guest/policy.py:197 _protected; agent:src/agent_guest/sandbox/unit.py:172 properties | E2E-34 (final matrix), ATK-13 (ATK on the end state), ATK-14 (ATK on the end state), ATK-15 (ATK on the end state), ATK-16 (ATK on the end state), ATK-17 (ATK on the end state), ATK-18 (ATK on the end state), ATK-19 (ATK on the end state), ATK-20 (ATK on the end state) | proven |
| R-10.6 | Subagents may be requested via a controlled platform operation and started by the platform. | agent:src/agent_runner/subagents/executors.py:17 request_subagent; agent:src/agent_runner/subagents/controller.py:122 Subagents; agent-core:src/agent_hub/operations/children.py:47 create_child | E2E-24 (final matrix) | proven |
| R-10.7 | Hooks block forbidden actions before execution. | agent:src/agent_guest/hooks.py:230 _pre_tool_use; agent:src/agent_guest/policy.py:242 Policy.evaluate | E2E-23 (final matrix), ATK-13 (ATK on the end state) | proven |
| R-10.8 | The sandbox enforces the bans technically as well. | agent:src/agent_guest/sandbox/landlock.py:406 apply; agent:src/agent_guest/sandbox/unit.py:172 properties; agent:src/agent_guest/hooks.py:163 fault_active | ATK-21 (ATK on the end state), ATK-22 (ATK on the end state), ATK-23 (ATK on the end state), ATK-24 (ATK on the end state), ATK-25 (ATK on the end state), ATK-26 (ATK on the end state), ATK-27 (ATK on the end state), ATK-28 (ATK on the end state), ATK-29 (ATK on the end state), ATK-30 (ATK on the end state), ATK-31 (ATK on the end state), ATK-32 (ATK on the end state), E2E-34 (final matrix) | proven |
| R-10.9 | Only explicitly defined, authorised actions; no arbitrary shell execution. | agent-core:src/agent_hub/operations/types.py:16 OPERATION_TYPES; agent:src/agent_runner/operations/checks.py:76 TYPES | E2E-22 (final matrix), ATK-33 (ATK on the end state) | proven |
| R-10.10 | Actions are checked deterministically and formally, not by content. | agent:src/agent_runner/operations/checks.py:180 check; agent-core:src/agent_hub/operations/register.py:288 register | E2E-22 (final matrix) | proven |
| R-10.11 | Execution by controlled platform components. | agent:src/agent_runner/operations/pipeline.py:473 Pipeline._execute; agent:src/agent_runner/github/executors.py:11 EXECUTORS | E2E-21 (final matrix) | proven |
| R-10.12 | Blocked attempts are logged. | agent:src/agent_guest/hooks.py:230 _pre_tool_use (session.hook); agent:src/agent_runner/sessions/journal.py:163 _hook_denied; agent-core:src/agent_hub/outbox/handlers_audit.py:66 handle | E2E-23 (final matrix), E2E-15 (final matrix) | proven |
| R-11.1 | Every operation requested by a session gets its own database record with unique ID, task/session reference, t… | agent-core:src/agent_hub/db/migrations/0001_hub.sql:410 (hub.operation); agent-core:src/agent_hub/operations/register.py:288 (register: INSERT + encrypted params event); agent:src/agent_runner/store/migrations/0001_runner.sql:115 (runner.operation); agent:src/agent_guest/schema.sql:43 (guest pending) | E2E-21 (final matrix), E2E-22 (final matrix) | proven |
| R-11.2 | The platform validates operations formally. | agent:src/agent_runner/operations/checks.py:180 (check: schema + type rules); agent:src/agent_guest/mcp_tools/request_operation.py:53 (local_check) | E2E-22 (final matrix) | proven |
| R-11.3 | … checks permissions. | agent-core:src/agent_hub/operations/register.py:246 (_refusal H1-H4: rights.can WG_WRITE, unknown_type, limits) | E2E-22 (final matrix), E2E-18 (final matrix) | proven |
| R-11.4 | … detects obvious duplicates by deterministic rules. | agent:src/agent_runner/operations/dedupe.py:48 (dedupe_slot, HMAC tag); agent-core:src/agent_hub/operations/dedupe.py:48 (claim slot); agent-core:src/agent_hub/operations/types.py:30 (WINDOWS per type) | E2E-22 (final matrix) | proven |
| R-11.5 | Example: the same comment request twice for the same PR is not published twice. | agent:src/agent_runner/crypto/dedupe.py:90 (github.comment fields repo/number/sha256(body)); agent-core:src/agent_hub/operations/types.py:30 (github.comment window 86400); agent:src/agent_runner/operations/pipeline.py:591 (_duplicate: not executed again) | E2E-22 (final matrix) | proven |
| R-11.6 | Idempotency key; retry rules per operation type. | agent:src/agent_guest/mcp_tools/request_operation.py:77 (optional idempotency_key); agent:src/agent_runner/operations/dedupe.py:28 (fields_for: idempotency fields); agent:src/agent_runner/operations/pipeline.py:486 (_attempts: RETRY_DELAYS 2/8/30 s for retryable failures) | E2E-22 (final matrix) | proven; recorded deviation: three retries = four attempts (register 03 §9) |
| R-11.7 | Rejected and deduplicated requests are audited too. | agent-core:src/agent_hub/operations/steps.py:52 (audit_step operation.<state> incl. rejected/denied/duplicate); agent-core:src/agent_hub/operations/register.py:288 (audit_step called for every decision) | E2E-22 (final matrix), E2E-15 (final matrix) | proven |
| R-11.8 | PR creation: session requests `create_pull_request`; platform checks formally, creates the PR with the connec… | agent:src/agent_runner/github/pulls.py:182 (create_pull_request with connected account, returns number/URL); agent:src/agent_runner/github/executors.py:15 (github.create_pull_request executor) | E2E-21 (final matrix), LIVE-02 (LVT-01) | proven |
| R-11.9 | Detailed relational PR data model deliberately deferred. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:410 (PR data only via operation.result_event_id, no PR table in hub schema) | E2E-21 (final matrix) | proven |
| R-12.1 | One whiteboard each on organization, workgroup and task level. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:465 (whiteboard_entry level org/workgroup/task); agent-core:web/src/core/whiteboards/location.ts:24 (orgBoard/wgBoard/taskBoard); agent-core:web/src/ui/whiteboard/Board.tsx:19 | E2E-25 (final matrix) | proven |
| R-12.2 | No project level. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:465 (CHECK level IN org/workgroup/task); agent-core:web/src/core/whiteboards/location.ts:13 (LEVELS, no project level) | E2E-25 (final matrix) | proven |
| R-12.3 | Users with write rights may edit entries. | agent-core:src/agent_hub/log/events.py:85 (KIND_RULES whiteboard.*: org scope ORG_ADMIN, wg scope WG_WRITE); agent-core:web/src/core/whiteboards/location.ts:64 (canWriteBoard) | E2E-25 (final matrix) | proven |
| R-12.4 | Users with read rights may view them. | agent-core:src/agent_hub/whiteboards/routes.py:62 (GET /v1/whiteboards org:member / wg:read); agent-core:src/agent_hub/whiteboards/read.py:67 (list_entries visibility); agent-core:web/src/core/whiteboards/location.ts:57 (canReadBoard) | E2E-25 (final matrix) | proven |
| R-12.5 | Changes are encrypted. | agent-core:web/src/core/whiteboards/actions.ts:151 (seal: encrypt with scope key + sign); agent-core:web/src/core/whiteboards/actions.ts:223 (createEntry) | E2E-25 (final matrix), E2E-09 (final matrix) | proven |
| R-12.6 | … synchronised immediately. | agent-core:src/agent_hub/stream/routes.py:33 (GET /v1/stream SSE fan-out); agent-core:web/src/core/net/sse.ts:199 (LiveChannel); agent-core:web/src/core/whiteboards/project.ts:189 (projectBoard) | E2E-25 (final matrix), PERF-03 (exclusive runs; T2 at the bound) | proven |
| R-12.7 | … logged with full history. | agent-core:src/agent_hub/whiteboards/routes.py:97 (GET /v1/whiteboards/{entry}/history); agent-core:src/agent_hub/whiteboards/read.py:109 (history from events); agent-core:web/src/ui/whiteboard/History.tsx:28 | E2E-25 (final matrix) | proven |
| R-12.8 | When creating an entry the author may trigger a notification to the authorised participants of that level. | agent-core:web/src/ui/whiteboard/Editor.tsx:17 (notify checkbox on new entry); agent-core:src/agent_hub/notifications/rules.py:148 (notify_whiteboard: in-app notifications to authorised recipients) | E2E-26 (final matrix) | proven |
| R-12.9 | Active cloud sessions get only a content-free ping. | agent-core:src/agent_hub/notifications/pings.py:47 (create_whiteboard_pings, content-free); agent:src/agent_runner/pings/hints.py:57 (ping hint to guest); agent:src/agent_guest/pings.py:46 (PingStore) | E2E-26 (final matrix) | proven |
| R-12.10 | The session decides itself whether to fetch the entry via the platform API; no automatic interruption or cont… | agent:src/agent_guest/hook_addons/pings.py:19 (notice only as additionalContext of next hook answer, never Stop); agent:src/agent_guest/mcp_tools/get_whiteboard.py:18 (content only via get_whiteboard) | E2E-26 (final matrix) | proven |
| R-13.1 | A running session can request a subagent via the platform (e.g. PR review). | agent:src/agent_guest/mcp_tools/request_operation.py:68 (subagent.request via platform tool); agent:src/agent_runner/subagents/executors.py:17 (request_subagent executor) | E2E-24 (final matrix) | proven |
| R-13.2 | The request is logged as its own uniquely identified operation and checked deterministically for rights and a… | agent-core:src/agent_hub/operations/register.py:288 (own operation row + H1-H4); agent-core:src/agent_hub/operations/register.py:233 (_limits for subagent.request); agent:src/agent_runner/operations/checks.py:106 (subagent.request TypeRules) | E2E-24 (final matrix) | proven |
| R-13.3 | Only the platform may start the subagent; Claude Code never directly. | agent:src/agent_guest/policy.py:242 (Policy.evaluate: Task/Agent and every non-file tool denied tool_forbidden); agent:src/agent_runner/subagents/session_request.py:15 (amend: depth>0 session not requested by controller refused) | E2E-24 (final matrix), ATK-02/ATK-03 (Agent, Task; the reference to ATK-05, WebSearch, was corrected by KOR-40) (ATK on the end state) | proven |
| R-13.4 | Every approved subagent is a full Claude Code session in its own sandbox with own harness, hooks, real-time s… | agent:src/agent_runner/subagents/controller.py:159 (start_child via SessionService.schedule_start: own unit/sandbox, hooks, sync); agent:src/agent_runner/sessions/service.py:102 (schedule_start) | E2E-24 (final matrix), E2E-17 (final matrix) | proven |
| R-13.5 | The new session is linked to the parent task and the requesting session. | agent-core:src/agent_hub/operations/children.py:47 (create_child: same task_id, parent_session_id, origin_operation_id); agent-core:src/agent_hub/db/migrations/0001_hub.sql:356 (parent_session_id column) | E2E-24 (final matrix) | proven |
| R-13.6 | Users can view the complete subagent session at any time in the web UI incl. read-only terminal view and hist… | agent-core:web/src/ui/screens/session/SessionScreen.tsx:78 (session detail: read-only terminal, chat, children); agent-core:web/src/ui/screens/session/children/ChildrenTab.tsx:90; agent-core:web/src/ui/task-tabs/sessions/SessionsTab.tsx:95 (session tree) | E2E-24 (final matrix) | proven |
| R-13.7 | Subagents can request further subagents only via the platform. | agent-core:src/agent_hub/operations/limits.py:85 (subagent_limit: depth counted by hub); agent:src/agent_guest/policy.py:242 (child sessions run the same policy, only platform tools) | E2E-24 (final matrix) | proven |
| R-13.8 | Limiting parallel or nested subagents is open. | agent-core:src/agent_hub/operations/limits.py:30 (MAX_DEPTH 2, MAX_RUNNING 3, MAX_TOTAL 10); agent-core:src/agent_hub/operations/limits.py:85 (subagent_limit); agent:src/agent_runner/subagents/controller.py:159 (depth re-check) | E2E-24 (final matrix) | proven |
| R-14.1 | Passkey PRF identity bundle, recovery code, scope keys with epochs, HPKE wraps, signed membership log. | agent-core:web/src/core/crypto/bundle.ts:52 (sealBundle, PRF identity bundle); agent-core:web/src/core/crypto/wrap.ts:43 (HPKE wrapKey per scope/epoch); agent-core:src/agent_hub/log/verify.py:431 (verify_log signed membership log); agent-core:src/agent_hub/crypto/recovery.py:24 (recovery code) | E2E-30 (final matrix), E2E-31 (final matrix), CON-02 (gates) | proven |
| R-14.2 | Session isolation in one VM and full execution ban: two independent layers. | agent:src/agent_guest/policy.py:242 (layer 1: PreToolUse policy); agent:src/agent_guest/sandbox/landlock.py:406 (layer 2: Landlock exec ban); agent:src/agent_guest/sandbox/unit.py:172 (per-session user/unit properties) | ATK-01 (ATK on the end state), ATK-02 (ATK on the end state), ATK-03 (ATK on the end state), ATK-04 (ATK on the end state), ATK-05 (ATK on the end state), ATK-06 (ATK on the end state), ATK-07 (ATK on the end state), ATK-08 (ATK on the end state), ATK-09 (ATK on the end state), ATK-10 (ATK on the end state), ATK-11 (ATK on the end state), ATK-12 (ATK on the end state), ATK-13 (ATK on the end state), ATK-14 (ATK on the end state), ATK-15 (ATK on the end state), ATK-16 (ATK on the end state), ATK-17 (ATK on the end state), ATK-18 (ATK on the end state), ATK-19 (ATK on the end state), ATK-20 (ATK on the end state), ATK-21 (ATK on the end state), ATK-22 (ATK on the end state), ATK-23 (ATK on the end state), ATK-24 (ATK on the end state), ATK-25 (ATK on the end state), ATK-26 (ATK on the end state), ATK-27 (ATK on the end state), ATK-28 (ATK on the end state), ATK-29 (ATK on the end state), ATK-30 (ATK on the end state), ATK-31 (ATK on the end state), ATK-32 (ATK on the end state), ATK-33 (ATK on the end state), ATK-34 (ATK on the end state), ATK-35 (ATK on the end state), ATK-36 (ATK on the end state), ATK-37 (ATK on the end state), ATK-38 (ATK on the end state), ATK-39 (ATK on the end state), ATK-40 (ATK on the end state), E2E-10 (final matrix) | proven |
| R-14.3 | VM start < 10 s via golden image + warm pool, clone fallback. | agent:src/agent_runner/vm/pool.py:515 (Pool.take warm pool); agent:src/agent_runner/vm/lifecycle.py:154 (_clone fallback); agent:src/agent_runner/vm/golden.py:399 (golden image Builder) | PERF-01 (exclusive runs), PERF-02 (exclusive runs) | proven |
| R-14.4 | Claude Code + OpenRouter + DeepSeek compatible; costs from the stream; quotas via usage endpoint. | agent:src/agent_guest/gateway.py:181 (UsageMeter: costs from the stream); agent:src/agent_runner/quota/subscription.py:86 (SubscriptionPoller usage endpoint); agent:src/agent_runner/quota/openrouter.py:34 (SpendPoller) | LIVE-01 (LVT-01), LIVE-03 (LVT-01), E2E-16 (final matrix) | proven |
| R-14.5 | API contracts, formal rules, idempotency windows. | agent-core:contracts/ops/dedupe-fields.json (windows, idempotency rule); agent-core:contracts/ops/rules.json; agent:src/agent_runner/operations/checks.py:180 (formal rules) | CON-01 (gates), E2E-22 (final matrix) | proven |
| R-14.6 | Device assignment, resource planning, offline/reconnect sync. | agent-core:src/agent_hub/scheduler/core.py:138 (choose: device assignment/resource planning); agent:src/agent_runner/sync/ws.py:79 (WsClient reconnect); agent:src/agent_runner/sync/outbox.py:66 (durable outbox for offline) | E2E-14 (final matrix), E2E-38 (final matrix) | proven |
| R-14.7 | PR tables deferred. | agent-core:src/agent_hub/db/migrations/0001_hub.sql:410 (no PR table; PR data only in encrypted operation result) | E2E-21 (final matrix) | proven |
| R-15.1 | `DFXswiss/agent-core` hosts hub, web, contracts and E2E. | agent-core:src/agent_hub/app.py:74 (hub); agent-core:web/src/main.tsx (web); agent-core:contracts/VERSION; agent-core:.github/workflows/platform-hub.yml:6 (integration branch ad5f2026-agent-platform) | CON-04 (gates), E2E-32 (final matrix T2) | proven |
| R-15.2 | `DFXswiss/agent` hosts runner, guest, doubles, images. | agent:src/agent_runner/cli.py:125 (runner); agent:src/agent_guest/server.py:85 (guest); agent:src/agent_doubles/ (doubles); agent:images/golden.yaml (images); agent:.github/workflows/platform-runner.yml:8 (same branch) | CON-04 (gates), E2E-32 (final matrix T2) | proven |
| R-15.3 | `DFXswiss/server` monitoring stack is reused; changes only as prepared patches. | agent:deploy/monitoring/agent_monitoring/patch.py:224 (main: renders prepared patch for the server repo, git apply --check); agent:deploy/monitoring/patch/README.md | E2E-17 (final matrix) | proven |
| R-P.1 | The concept is implemented completely (all rows of this document). | agent-core:e2e/platform/traceability.json (matrix of all 153 rows); agent-core:e2e/platform/orchestrator/traceability.py:264 (check_suite) | GATE-MATRIX (this document) | proven |
| R-P.2 | New packages in the existing repos, consistent with the code base, reuse of proven building blocks, legacy co… | agent-core:src/agent_hub/ and agent:src/agent_runner/, agent:src/agent_guest/ (new packages); agent-core:tests/platform/test_legacy_untouched.py:126; agent:tests/platform/test_legacy_untouched.py:128 | CON-04 (gates), U-HUB-legacy-untouched (gates), U-RUN-legacy-untouched (gates) | proven |
| R-P.3 | 100 % line and branch coverage for all new code, technically enforced. | agent-core:pyproject.toml:71 (fail_under 100, branch true); agent:.coveragerc-platform:21 (fail_under 100); agent-core:web/vitest.config.ts:37 (100 % thresholds src/core); agent-core:Makefile:39 (platform-test, platform-e2e coverage) | GATE-COVERAGE (gates) | proven |
| R-P.4 | Every requirement has at least one E2E test. | agent-core:e2e/platform/orchestrator/traceability.py:264 (check_suite); agent-core:e2e/platform/orchestrator/prechecks.py:42 (trace pre-check at every E2E run) | GATE-TRACEABILITY (gates) | proven |
| R-P.5 | Hermetic main suite: real Claude Code against the model double, GitHub and mail doubles, passkey via virtual… | agent:src/agent_doubles/model/server.py, agent:src/agent_doubles/github/server.py (doubles); agent-core:e2e/platform/orchestrator/egress.py:208 (violations: network egress check); agent-core:e2e/platform/orchestrator/config.py:36 (topologies) | E2E-01 (final matrix), E2E-02 (final matrix), E2E-03 (final matrix), E2E-04 (final matrix), E2E-05 (final matrix), E2E-06 (final matrix), E2E-07 (final matrix), E2E-08 (final matrix), E2E-09 (final matrix), E2E-10 (final matrix), E2E-11 (final matrix), E2E-12 (final matrix), E2E-13 (final matrix), E2E-14 (final matrix), E2E-15 (final matrix), E2E-16 (final matrix), E2E-17 (final matrix), E2E-18 (final matrix), E2E-19 (final matrix), E2E-20 (final matrix), E2E-21 (final matrix), E2E-22 (final matrix), E2E-23 (final matrix), E2E-24 (final matrix), E2E-25 (final matrix), E2E-26 (final matrix), E2E-27 (final matrix), E2E-28 (final matrix), E2E-29 (final matrix), E2E-30 (final matrix), E2E-31 (final matrix), E2E-32 (final matrix T2), E2E-33 (final matrix), E2E-34 (final matrix), E2E-35 (final matrix), E2E-36 (final matrix), E2E-37 (final matrix), E2E-38 (final matrix), E2E-39 (final matrix) | proven |
| R-P.6 | A few live tests (OpenRouter/DeepSeek, GitHub test repository, subscription read-only). | agent-core:e2e/platform/live/driver.py:312 (operator driver for live runs) | LIVE-01 (LVT-01), LIVE-02 (LVT-01), LIVE-03 (LVT-01), LIVE-04 (not run – optional, not needed) | proven |
| R-P.7 | Topologies T1 (single device) and T2 (hub on second device, runner on runner device). | agent-core:e2e/platform/orchestrator/config.py:36 (TOPOLOGIES t1/t2; build_config:101) | E2E-32 (final matrix T2) | proven |
| R-P.8 | Everything built and tested on localhost / work devices; no production deployment, no tunnel changes. | agent-core:e2e/platform/orchestrator/egress_report.py:165 (build: LAN-only/no-tunnel report); agent-core:e2e/platform/orchestrator/egress.py:208 | E2E-32 (final matrix T2) | proven |
| R-P.9 | Public-repo hygiene: no internal host names, IPs or secrets in code, docs, fixtures or commits. | agent-core:tests/platform/test_hygiene.py:178 (scan of new paths); agent:tests/platform/test_hygiene.py:181 | U-HUB-hygiene (gates), U-RUN-hygiene (gates) | proven |
| R-P.10 | Existing Claude subscription profiles are never modified, logged out, re-logged or refreshed; only the curren… | agent:scripts/probes/probelib/live03.py:111 (access_token: never refreshes); agent:scripts/probes/probelib/live03.py:97 (compare_snapshots profile + keychain); agent-core:e2e/platform/live/driver.py:118 (compare before/after) | LIVE-03 (LVT-01) | proven |
| R-P.11 | Work on integration branches with draft PRs; no merge into `develop`, no force push. | agent-core:docs/platform/acceptance/M3.md:177 (manual record: develop unchanged, no force push, only draft PRs) | GATE-BRANCH-PROTECTION (R-P.11 check) | proven; evidence by design an acceptance check (section "R-P.11" of FINAL.md) |
| R-P.12 | Platform devices and the platform model budget only for entitled orgs (private orgs from a platform invitatio… | agent-core:src/agent_hub/orgs/logs.py:179 (platform_access: private org or org_platform_access); agent-core:src/agent_hub/orgs/append.py:288 (platform_access_denied); agent-core:src/agent_hub/platform/cli.py:419 (grant-org-access) | E2E-01 (final matrix), E2E-05 (final matrix) | proven |
| R-P.13 | Platform model budget per org and day (`PLATFORM_BUDGET_USD_PER_ORG_DAY`, `budget_exceeded`). | agent-core:src/agent_hub/usage/budget.py:62 (check, PLATFORM_BUDGET_USD_PER_ORG_DAY); agent:src/agent_guest/gateway.py:393 (429 budget_exceeded); agent:src/agent_runner/subagents/op_prechecks.py:31 (budget_exceeded) | E2E-27 (final matrix) | proven |

## Independent checks

- **Sample of 24 requirements** (R-1.3, R-1.7, R-1.9, R-2.1, R-2.5, R-2.8, R-3.1, R-3.12, R-3.13, R-4.4, R-4.7, R-7.3,
  R-7.6, R-8.9, R-9.4, R-9.7, R-10.6, R-10.9, R-11.6, R-12.8, R-13.5, R-15.2, R-P.4, R-P.8), given only the
  requirement text and the code: 17 met, 7 partially met, none not met. Handled: R-2.1 (no E2E clicked a suggestion
  card → E2E-06 asserts it, decision 6), R-P.4 (gate at milestone 2 → milestone 3, decision 4), R-9.7 (tag, decision
  4); recorded as deviations: R-4.4 (page recognition of the mailbox automation), R-8.9 (`/v1/stream` writes no read
  row; best-effort read log), R-11.6 (three retries, four attempts) – decision 8, register; R-7.3 (retention only in
  unit tests) accepted: the 48 h / 90 d rollup retention is proven by `tests/platform/hub/monitoring/test_rollup.py`.
- **Register** (`spec-deviations.md`, decision 9): 31 "Unverified" bullets checked against the code, 16 of 19
  deviation rows checked; one medium product defect found and fixed (quota polling, decision 10).
- **Review of this package's commits** by an independent reviewer: two medium findings (a new path to a false
  refusal audit in the quota fix; register rows citing this document before it existed) and lower ones; all medium and
  the relevant low ones fixed (decision 10; this document and the decision file merge with the register rows).

## Documentation

Both repositories hold the same `docs/platform/` set apart from repository-specific evidence: specification
`spec/00`…`10`, 50 ADRs (`adr/001`…`050`, no duplicate number), runbooks (claude-code upgrade gate, device
certification, export/restore, golden image release, hub operation, local development and tests, monitoring patch
handover, platform root key, runner installation), `PLATFORM-DESIGN.md`, `CUTOVER.md` (including "Compression is the
edge's job", AUD-01 B-26), the register `spec-deviations.md`, the PRF matrix `manual-checks/0.1.0.md`, decisions and
acceptance evidence; `performance/results.md` lies in agent-core. Equality: the sibling-checkout test above.
Consistency with the code: the register check (decision 9) and the requirement table.

## Scans

- History of both integration branches since the `develop` branch point (agent 548 commits, agent-core 707 commits at
  the start of this package; commit messages and every added line) with the rules of both hygiene tests: 113 pattern
  hits, the same set ACC-M3 classified (attributes named `local`, test fakes, documentation addresses, a placeholder
  home, a removed profile-name comment); no new hit, no secret, no device name, no device address, no account.
  Repeated on the heads of round 2 (agent 554, agent-core 716 commits): the same 113 hits, nothing new.
- Every commit signed (`%G?` = `G`), one author identity (no-reply address).
- Hygiene tests of the file state green in every gate; the PR texts checked against the same rules.

## R-P.11

- `develop`: agent `509198a5`, agent-core `2ad6f724` – the states at the start of the project (read from the remote).
- `board/merges.log` of the workspace: every recorded integration head is an ancestor of the current head, no
  "force" entry: no force push on the integration branches.
- Pull requests from `ad5f2026-*`: only the two drafts, open, not merged; no other pull request of the project.
- Repository rules: `develop` of agent-core has rules (no deletion, no non-fast-forward, pull requests only); `develop`
  of agent shows none – an owner setting (item for a person).
- GitHub Actions created no run for the integration branches (AUD-01 C-05, item for a person).

## Defects

Open defects of severity high or medium: none. **PERF-03 on T2** missed its bound in one of two exclusive runs (303.2 ms; tail on the wireless link between the devices, hub share 8 ms); fixed by the hedged outbox push (decision 12, ADR-050): 192 ms on the final pair, on a link of the same quality. Fixed by this acceptance: quota polling could stop when the hub
elected a device without the owner's recent sessions (medium, decision 10); a lazily loaded chunk that could not be
fetched while the link was down failed the whole screen (medium, decision 11). Recorded low deviations: see the register.

## Residual risks and items for a person

- **PRF device matrix** (AUD-01 B-12): 9 manual rows of `docs/platform/manual-checks/0.1.0.md` on real platforms
  (iCloud Keychain, Google Password Manager, Windows Hello, 1Password, Bitwarden, YubiKey); the suite uses the virtual
  authenticator.
- **Never exercised live** (AUD-02, B-13): the GitHub device flow against GitHub, the keychain isolation of
  `claude setup-token`, a setup token at the usage endpoint; the mailbox automation against the real authorize pages
  (falls back to the relay, decision 8). **Since KOR-21** the relay has run against the real `claude setup-token`
  (Claude Code 2.1.330, no account, a deliberately wrong code): the authorize URL arrives through `BROWSER`, the input
  field is recognised on the reconstructed screen and the code is typed only then, the refusal ends the login with
  `code_invalid` about 0.2 s after typing while the program is still open (ADR-051; a raw recording of the program is
  a test fixture). **Still never live:** the success path (the token screen of the real program, the stored
  credential, its use) – it needs an account and is covered by screens drawn the way 2.1.330 draws; the keychain
  isolation of a completed login; the mailbox automation against the real pages.
- **Re-keying after the loss of the platform root key** never rehearsed (NB-12; runbook `platform-root-key.md`).
- **Static Landlock launcher** (B-10) deliberately not built (ATS-03 decision 10).
- **E2E only against the TEST_MODE golden** (B-11): the production golden is built and checked by ATS-03, the suite
  runs the test image.
- **Release-like build**: the smoke against the minified bundle uses the test root key; the release build with the
  production key is part of the cutover (decision 7); SRI of lazy chunks is a step of the manual release check list
  (NB-10).
- **A-30 under a burst** (PFM-01 decision 5): the start estimate shown for many simultaneous first tasks is a
  per-session estimate. **B-19**: terminal events need about 14 MB per session hour of continuous output at the hub;
  no retention rule.
- **Scheduler** (KOR-14): a heartbeat measured before a refusal but received after it lifts the refusing device's
  block early; bounded by three refusals in a row.
- **Clone budget** (KOR-15): the clone step holds 5.85–5.96 s against its 6 s share; on a heavily loaded device the
  boot alone can exceed 6 s, the 10 s total stays met.
- **`upstream_unreachable` at GitHub connect** (KOR-16, cause found and fixed by KOR-18: idle hub connections closed
  by the hub while the runner reused them): in this acceptance's T2 lanes no `github_connect retry`, no
  `github_connect failed`, no `push_failed` on a reused connection; the only `push_failed reason=hub_unreachable`
  lines fall into E2E-14's deliberate offline window. Not checked for the same pattern: the test relays
  `orchestrator/live_bridge.py` and `tunnels.py`.
- **Credential records ahead of the log copy** (KOR-17): a record of a user or device that just joined can be refused
  as `key_unknown` when it arrives before the log copy shows its author (`credentials/cache.py`); the quota poller now
  refreshes the logs first (decision 10), other callers do not; never observed.
- **PERF-03 on T2 depends on the wireless link** (decision 12): with the hedged push 192 ms at a link mean of 68 ms;
  the copy costs the hub one more short transaction per hanging push (at most one per 2 s and device). When the copy's
  answer counts, items the hub rejected come back `replayed` and the runner's `item_rejected` log line is missing for
  them (the hub's own audit of the rejection is unaffected, ADR-050).
- **Tooling** (AUD-02 C-19): `tools/wp/merge.sh` rejects neither diverging `contracts/VERSION` files nor duplicate
  ADR numbers across repositories; the workspace's own ADR folder has two ADR-044 (outside the repositories).
- **Branch protection** of `develop` in agent and **GitHub Actions** (C-05): settings of the owner.

## Corrections made by ACC-FINAL

One commit with its test per fix.

| Repository | Commit (subject) | Fix | Test |
|---|---|---|---|
| agent | Poll the subscription quota of every owner the hub elects this device for and fetch an uncached record. | quota polling stopped when the elected device had no recent session of the owner (medium, decision 10) | `tests/platform/runner/quota/test_subscription.py` (red before); E2E-16 |
| agent | Refresh the logs before the quota poller judges a record it fetches unprompted. | no false `runner.inbound_rejected` for an owner ahead of the log copy (review finding, decision 10) | `test_the_record_of_an_owner_the_log_copy_does_not_show_yet_is_fetched_after_a_refresh` (red before: `key_unknown` audited) and three further cases |
| agent-core | Require every spec test a requirement lists to carry its tag and add the ten missing tags. | the traceability report follows the markers in both directions (decision 4) | `test_a_listed_spec_test_must_carry_the_requirement_tag` (red before) |
| agent-core | Trace the visible capacity refusal of the rescheduling case of E2E-38 to R-3.12 and R-7.8. (agent: Trace R-3.12 also to the rescheduling case of E2E-38.) | KOR-14's case tagged (decision 5) | traceability gate; E2E-38 |
| agent-core | Raise the traceability gate to milestone 3 now that M3 is accepted. / Pin the traceability gate to the last accepted milestone and correct register references. / Let the relay test's target answer only after the relay recorded the forced end. | gate at milestone 3 (decision 4) | `test_traceability.py` |
| agent-core | Assert in E2E-06 that a suggestion chip fills and focuses the composer. | R-2.1 asserted end to end (decision 6) | E2E-06 desktop and mobile |
| agent-core | Let an E2E run serve the minified production web build with E2E_WEB_MINIFY=1. | release-like smoke (AUD-01 B-26, decision 7) | `tests/test_config.py`, `tests/test_run.py` (red before); E2E-00/-01/-06 against the minified build green |
| agent-core | Show the plain fallback of a lazily loaded part whose chunk cannot be fetched instead of failing the screen. | the screen no longer fails when a lazy chunk cannot be fetched (medium, decision 11) | `web/tests/ui/chunk-boundary.test.tsx`; `web/e2e/devices/lazy-unavailable.spec.ts` (red before); E2E-14 |
| agent-core | Let only an unavailable chunk fall back and give the terminal its own message for it. / Expect the chart's sparkline instead of the error page when its lazy stylesheet is refused. | review findings on the fix: other errors still reach the error page; own terminal message (decision 11) | `tests/core/assets/integrity.test.ts` (typed refusal), `tests/ui/chunk-boundary.test.tsx` (other errors pass through, red before); `lazy-unavailable`, `lazy-stylesheet`, `lazy-integrity` specs |
| agent | Send an outbox push whose answer is late once more on another connection. / Send at most one copy of a late push every two seconds and none for a large batch. | PERF-03 on T2 missed its bound (303.2 ms): a push hanging on the wireless link held every later item (decision 12, ADR-050) | `tests/platform/runner/sync/test_push_hedge.py` (10 cases); exclusive PERF T1/T2 on the final pair |
| both | Record the hedged outbox push in the specification and in ADR-050. / Bound the hedged outbox push in the specification and in ADR-050. | specification 05 §2.3, 03 §1, ADR-048 note, ADR-050 | `tests/platform/test_docs.py` (sibling checkout) |
| both | Settle the unverified register items against the code and correct two specification references. (and the reference corrections) | register and specification (decision 9) | `tests/platform/test_docs.py` |
