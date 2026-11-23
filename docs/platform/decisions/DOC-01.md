# DOC-01 decisions

Documentation, runbooks, final PLATFORM-DESIGN and the PRF device matrix (09-milestones.md WP-M3-06, WP-M3-07).
The same file is in both repositories.

1. **One documentation tree in both repositories.** `docs/platform/spec/`, `adr/`, `PLATFORM-DESIGN.md`,
   `README.md`, `spec-deviations.md`, `runbooks/` and `manual-checks/` are identical in agent and agent-core (before
   DOC-01 each copy had newer passages – 05 §4.3 in agent, 07 §5.2 in agent-core – and the session-token ADR existed only in agent;
   AUD-01 B-31; the newer text of each was kept). `tests/platform/test_docs.py` enforces that the shared files link only
   inside `docs/platform/` and, when the other repository is checked out beside this one (or `PLATFORM_SIBLING_CHECKOUT`
   names it), that they are byte-identical; without a sibling checkout that check is skipped with its reason. Links in these files
   stay inside `docs/platform/`; paths of the other repository are written as code, never as links, so the link
   check passes in both. `decisions/` and `acceptance/` stay per repository. The workspace copy of the specification
   and the ADRs (outside the repositories) was synchronised with the same text, including ADR-030 … ADR-044.
2. **As-built register instead of rewriting every section.** About 200 refinements and deviations recorded in the
   package decisions were swept (all `decisions/*.md` of both repositories and `contracts/changes/`), checked against
   the code and collected in `spec-deviations.md` with the handling `as built`, `deviation`, `open defect` or
   `spec updated`. Every specification document links to its part of the register in its header. The specification
   text itself was changed only where it was wrong or misleading for operators and reviewers: AUD-01 B-31 (ADR-027 §4
   registries, 05 §4.3 session user and hidden system sockets, 02 runner and guest `ping`, 03 §9 409/422, 07 §3
   coverage sources and justified exclusions), A-08 (06 §10.2, 04 §9), D-03 (03 §2, one hub process), the 00 §5/§6
   layout and branch rules (ADR-027 §1) and a wrong cross reference in 05 §2.2.
3. **ADR-027 amended, not superseded.** The extension points added by later packages are listed in an amendment
   section with the deciding package or ADR; the original table stays as the M0 plan. The point
   `model_access.py:STRATEGIES` (ADR-032, RUN-04) was removed by RUN-12 and is named as removed.
4. **SRI of lazily loaded chunks (A-08).** 04 §9 ("SRI for all assets") and 06 §10.2 (only `index.html` tags)
   contradicted each other. The threat model wins: every script the hub delivers must be integrity-checked, lazy chunks
   included; the build must fail otherwise. The implementation and the browser test with a manipulated chunk belong
   to KOR-04; until then the register lists it as `open defect`.
5. **Automated PRF rows as a device spec.** `web/e2e/devices/prf-matrix.spec.ts` (agent-core, new file outside
   `docs/platform/` because the package requires the automatable rows to be executed) runs nine rows: desktop,
   Pixel 7 and iPhone 15 emulation × platform passkey with PRF, without PRF, and a roaming `usb` security key with
   PRF, each through registration, login, PRF state, countdown, terminal and recovery. It lives in `e2e/devices/`
   (discovered by the `devices` project) and switches the device profile per `describe` block without the browser
   type, so `playwright.config.ts` stays unchanged and the rows run in every `npm run test:e2e`.
6. **Root-key loss has no in-place recovery.** The platform log has no rotation entry and membership logs reference
   platform-log positions; the runbook therefore treats re-keying as a new platform (new build, new database,
   re-enrolment). The procedure is documented, not rehearsed.
7. **Export/restore runbook ahead of DR-01.** The runbook follows the interface of the DR-01 branch
   (`agent-hub export|restore|rebuild-projections`) and says so; DR-01's acceptance is the evidence.
8. **Rate limits are documented as they are.** The anonymous limit (30/min per TCP peer, forwarding headers not
   trusted) is a constant; the runbook describes effect, symptom and the missing configuration (AUD-01 A-11 stays
   open) instead of inventing a setting.
9. **Orchestrator clock route.** `e2e/platform/orchestrator/routes/clock.py` no longer promises a fake clock from
   SCN-03 (AUD-01 C-15); the route stays `501` and its docstring gives the reason (SCN-03 decision 1).
10. **Manual-check file name.** `manual-checks/0.1.0.md` after the hub and web version of the integration branch.
11. **Review follow-ups.** Golden rollback is a rebuild from the previous commit (a retired golden cannot be
    reactivated); the anonymous rate limit behind the mandatory TLS front end is recorded as a production blocker
    until A-11 (KOR-06); the PRF matrix also proves that the old recovery code is refused after a recovery.
12. **ADR number collision.** KOR-01 accepted the invitation proof as ADR-043 in agent-core while ATS-02 had used
    ADR-043 for the session-token binding in agent. The invitation proof keeps 043 (cited in code, contracts and
    vectors of agent-core); the session-token binding becomes **ADR-044** (cited only in documentation; its file says
    so, and the ATS-02 decisions and `atk/M2.md` were updated with a note). Both ADRs are in both repositories.
13. **Corrections merged during DOC-01.** KOR-01 (A-02: `inv_sig`), KOR-04 (A-07: `cred_holders`), KOR-06 (trusted
    proxy settings, job `at_start`) and KOR-07 were merged before DOC-01; their documentation needs are taken into
    03 §2/§4.1, 04 §3/§4.8/§5.1, 02 §2.2, 06 §11, ADR-027 and the runbooks (the bootstrap-invitation restriction and
    the proxy blocker are lifted). A-08 (SRI of lazy chunks) is still open in KOR-04.
14. **Test helper paging.** The nine devices of the PRF matrix pushed the platform log of a full web-suite run past one
    page (200 entries); the helper `PageUser.platform()` (`web/e2e/helpers/user.ts`) read only the first page, so
    `devices/tasks.spec.ts` failed with `log_rights` in the gates. The helper now reads every page like the web client
    (`readLog` in `src/core/account/join.ts`); the product was not affected.
