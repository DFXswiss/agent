# Decisions of ACC-M3 (acceptance of milestone 3)

The same file exists in `agent-core` and `agent`. Evidence: `docs/platform/acceptance/M3.md`.

1. **The matrix is the E2E scenarios; PERF counts only when measured exclusively.** WP-M3-01 asks for every scenario
   of `07-testing.md` §2.1 on T1 and T2, desktop and mobile, three times. The PERF scenarios (§2.2) are performance
   gates: by the measurement rule of the work devices a PERF figure counts only from an exclusive run (no other run on
   the devices), and those runs belong to PFM-01 (and its acceptance to ACC-FINAL). Inside a busy matrix PERF-03 on T2
   misses its 300 ms bound (p95 348.6 ms at a device load of 16–18) while every E2E scenario of the same run is green.
   The functional lanes therefore run with `--only E2E-00,…,E2E-39` on desktop (the mobile project excludes `perf/`
   by its configuration); the PERF values the first whole-suite lanes produced are recorded as non-exclusive context.
2. **The M3 gate evidence is `docs/platform/acceptance/M3.md`.** `traceability.json` named
   `docs/platform/acceptance/ACC-M3.md` for `GATE-MATRIX` (R-P.1) and `GATE-BRANCH-PROTECTION` (R-P.11), while the
   acceptance documents of M0–M2 are named after the milestone (`M0.md` … `M2.md`) and the package names `M3.md`. The
   mapping now points at `M3.md` (a correction of the mapping, ADR-027 §4).
3. **The traceability gate of `make platform-ci` runs for milestone 2.** ACC-M2 kept the default at 1 because R-6.6
   (E2E-29) was missing; with SCN-08 merged `--milestone 2` is 147 of 147. Milestone 3 cannot be the default yet:
   `PERF-05` has no spec on the integration branch until PFM-01 is merged (decision 9).
4. **AUD-01 B-27 (requirements proven only in Vitest/jsdom).** Re-assessed on the current suite: the device choice at
   workgroup creation is driven in Chromium by E2E-38 (`workgroups.create.device.<id>`, only the chosen device is
   assigned and used); `platform_access_denied` is asserted by E2E-05 against the real hub until the operator grants
   `org_platform_access`; the scheduler's choice by capacity, workgroup VM and assigned devices is E2E-38. The one gap
   left were the log vectors: only the crypto vectors ran in Chromium. `web/e2e/core-interop.spec.ts` now bundles
   `tests/core/log/vector-suite.ts` as well and runs every log vector file (log, events, rotation, wraps, platform
   log; positive and negative) on the browser's own WebCrypto, with the same count of checks as Vitest.
5. **E2E-09 requires the export scan** (AUD-02 NB-02). Since DR-01 the canary scan reads one `agent-hub export` per
   organization; the four SCN-09 runs predate that. The spec now fails unless the scan read at least one export, each
   non-empty, and its note counts them. The first matrix lanes show 11–26 exports per run, 0 findings.
6. **On phones the toasts are the header's last row.** The toast list was fixed at the bottom left above the bottom
   bar, exactly where a page's last actions are once it is scrolled to the end (the chat composer's Send). A toast
   pauses its 6 s timer while the pointer is on it, so a tap aimed at Send kept it open and opened the notification
   instead; E2E-33 in the mobile project hung on this for its whole timeout. A first fix (fixed below the header,
   `top-16`) would cover the second row of a wrapped header (review). Below `md` the list is now the header's last
   row in the flow (`order-last basis-full`); while empty it is taken out of the flow (`empty:absolute`) so it adds no
   row but stays in the accessibility tree as the live region; from `md` on the toasts stay fixed at the bottom left.
   `web/e2e/notifications.spec.ts` checks at 360 × 640: the toast lies in the upper half, overlaps neither brand,
   bell, settings nor the bottom bar, the header grows by the toast while it is shown (in the flow, however many rows
   the header has) and the empty list adds no row; it fails for the earlier variants. Costs accepted: the header is
   not sticky, so a user scrolled far down sees the toast only through the live region, the bell badge and the
   notification center, and a toast shifts the page by its height for 6 s. The rotation and credential toasts
   (`rotation-toast.tsx`, `credentials.tsx`) stay fixed at the bottom right on phones: rare, right-aligned, low
   (left for ACC-FINAL).
7. **Flaky tests F3, F4, F5, F8 (AUD-02 §6).** F4 and F5 were product races in the guest and are fixed with a test
   that fails without the fix: a suspended run whose pane end the watcher saw only after the resume failed the resumed
   session (`agent_guest/sessions.py`); the control process installed its signal handlers only after its socket
   answered, so an early SIGTERM killed it instead of stopping it (`agent_guest/control.py`). F3 was a test race
   (`records[-1]` after terminal records); the test reads the session's own last state record. F8 was fixed by
   FIX-01 (`a7384afe`, the process-wide `umask` of in-process guests); with FIX-01's slow-bind reproduction the old
   harness fails 3/3 and the current one passes 3/3.
8. **History scan findings are false positives or remediated.** The scan of every commit since the `develop` branch
   point (messages and added lines, rules of both repositories' hygiene tests) reports Python attributes named
   `local` of key and request objects, fake test credentials (`sk-ant-…-not-a-real…`, `ghp_kz22Fake…`), RFC 1918
   example addresses of tests that were later replaced by documentation addresses, a placeholder home directory of a
   test and one comment naming a local profile directory (LIVE-03, removed in the same package). None is a secret, a
   host name or an address of the project's devices; the integration branches are not rewritten (no force push,
   R-P.11).
9. **PERF acceptance moves to ACC-FINAL when PFM-01 is not finished.** By the orchestrator's instruction of
   2026-11-23 00:30 PFM-01 measures PERF-01…PERF-05 exclusively in parallel to this package; the acceptance of the
   PERF results and of the final state after a possible runner optimisation lies with ACC-FINAL.
10. **LIVE-04 is not executed: optional, not needed.** There is no Anthropic API key by the owner's decision; R-4.3 is
    proven by E2E-27 (model double) and the live proof for Claude is LIVE-03 (subscription, read-only).
11. **E2E-33 counts only the runner audits raised after it started.** The spec asserted "no `runner.*` audit of the
    device", but the platform device is shared by the whole suite: a request of a user the runner had not caught up
    with yet is refused as transient (`key_unknown`) and audited once by design (`agent_runner/requests/service.py`,
    `orgs/audit.py`), then passes. One mobile T2 lane failed E2E-33 on such an audit of E2E-20/E2E-21. The spec now
    records the audit position (`max(seq)`) at its start and counts only newer rows; the restore keeps every audit
    row with its `seq` (the spec asserts the identical audit head), so an alarm caused by the restore or the store
    wipe stays visible.
12. **The GitHub device-code failure of the first mobile T2 lane did not recur.** In the first round (state before
    the fixes) E2E-32 and E2E-33 of one mobile T2 lane failed at "Connect GitHub" (`upstream_unreachable`; the
    double journaled the request, the relay saw the runner side close before the answer). E2E-20 and E2E-21 of the
    same lane, every desktop T2 lane and every later mobile T2 lane of the final matrix connected normally. Without a
    reproduction the cause stays open; it is listed as a residual risk for ACC-FINAL.
13. **R-P.11 is checked by the effect, not by a repository setting.** `develop` of both repositories equals the state
    recorded when the integration branches were created, every recorded integration head is an ancestor of the
    current one, and no pull request is merged. The repository settings differ: `develop` of agent-core is protected
    by rules (no deletion, no non-fast-forward, pull requests only), `develop` of agent shows no protection rule; this
    is the owner's setting and is named in the M3 acceptance for the cutover.
