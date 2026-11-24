# WCR-02 – TypeScript log verification, rights projection, rotation and catch-up planner

Decisions taken where `04-crypto.md` sections 4.3, 4.7, 4.8, 5.9-5.11, 6.1-6.4, `06-web.md` sections 1, 7.1, 11 and
the Python reference `agent_hub.log` left room.

1. **Parity by construction.** `web/src/core/log/` follows `agent_hub.log` module by module and check by check (same
   order, same codes, same short-circuits, e.g. the founder key is only decoded after `pub.sig == founder`). The
   vector context `tests/core/log/vector-suite.ts` is the twin of `agent_hub.log.vectors.Context`; all five log
   vector files are consumed (positive parts reproduced, all 135 negative vectors refused with their code).
2. **Module mapping.** `batch.py` (`check_batch`) lives in `rotation.ts` next to the builder (the package lists
   "R + Batch-Bau" for `rotation.ts`); `required_set` lives in `state.ts` and is re-exported by `rotation.ts`, so
   `verify.ts` (self-`remove` → `pending`) and `rotation.ts` (which uses `verify.ts`) do not import each other.
3. **Hub-side rules are ported too** (`appendEntries` with `now`/`platformAccess`, `checkBatch`, `checkEventHub`,
   `requireActive`): the vectors name them, and the browser uses them to predict the hub's answer (the builder runs
   `checkBatch` on its own result before anything is sent).
4. **Times are unix milliseconds** (`now`, `rotatedAt`, `GRACE_MS = 600 000`); header times are parsed with the
   core's `parseTime`. Nothing in the log core reads the clock except the builders' default `ts`.
5. **Batch builder.** `buildBatch` signs the requested entries, computes R from the resulting states exactly as
   `checkBatch` does (self-`remove` excluded), appends one `rotate` per element of R (sorted, after the requested
   entries), draws a fresh key for every epoch the batch creates and wraps it for every reader at the end
   (decertified platform devices excluded). In addition every principal the batch newly entitles to a scope that is
   not new (grant, device assignment incl. the org key of a first assignment per ADR-007, accept self-wraps) gets
   the current epoch if the author holds the key; with `history` the subject of a `grant` also gets the older
   epochs of that workgroup (per grant, not for every newly entitled principal; review finding). Wraps the author
   cannot create are left to catch-up. A new org is created by `genesis` alone (`POST /v1/orgs` carries one entry;
   `createOrgBody` maps the result). The builder refuses to run without the platform state once the org has
   platform devices (`excludedFor`, `log_rights`), so a decertified device is never a recipient by omission. Platform access (ADR-026) and invitation expiry are receipt-time checks of the hub;
   the client side check runs without `now` and the UI asks `PlatformState.platformAccess` before offering platform
   devices.
6. **`seq_conflict`.** The builder is a pure function of the verified log; WEB-06 re-reads the log and calls it again
   (max 3 attempts, 06-web.md section 7.1). Keys are drawn anew for every attempt.
7. **Pending rotations** (job 4) are a separate batch (`pendingRotation`): one `rotate` per pending scope the author
   may rotate (org: admin; wg: admin or writer); a removal batch with non-empty R may not carry other `rotate`
   entries (CRY-02 decision 6).
8. **Catch-up never trusts the hub's offer.** `confirmedSlots` keeps only well-formed slots of the **current** epoch
   of a scope of this log whose recipient is entitled now and that the user may create (W1/W2); older epochs are
   given only through an explicit grant with history (review finding: otherwise a hub could collect history keys
   for any member). The recipient key always comes from the verified log (`buildWraps`); `catchUpWraps` takes the
   platform state and refuses without it once the org has platform devices. `missingWraps`/`missingCredWraps` are
   the reference plans.
9. **Stored heads, rollback, fork.** `refreshLog({founder, head}, entries)` = L9 (`checkExtends`) + full L1-L8
   verification; `refreshPlatformLog(head, entries, root)` does the same for the platform log (06-web.md section 2.2
   stores its verified head; a hub dropping a later `device_decertify` is `log_rollback`); storage of the pinned founder and head is WEB-02's job. `resolveLh(log, lh, refresh)` implements L10
   with an injected refresh. The reader's incremental path `extendLog` reports a gap as `log_chain` (`seq_conflict`
   is the hub's append code).
10. **Platform root.** `verifyPlatformLog(entries, root)` takes the pinned root key as a parameter; the app passes the
    compiled `import.meta.env.VITE_PLATFORM_ROOT_KEY` in from `main.tsx`, exactly like the start-up config check of
    WEB-01 (`src/core` never reads the build environment; under Vitest the `import.meta.env` rewrite also adds a branch
    the merged coverage gate counts). A root key from the hub is never used (`imports.test.ts` forbids `import.meta`
    in the log core).
11. **Wrap verification job** (`verifyWraps`) returns the accepted wraps and the ignored ones with their code, so the
    UI can count `wrap_unauthorized` (06-web.md section 11 job 2).
12. **Reader E7** is applied only when the reader knows the session's device (`sessionDevice`), as in the reference.
    `openCheckedEvent` combines the reader checks with decryption under the scope key of the header's epoch.
13. **Vector consumption test.** `tests/core/crypto/vectors.test.ts` (WCR-01) excluded the log files until their
    consumer landed and asked that consumer to register; it now requires every vector file to be consumed by the
    crypto suite or the log suite (`LOG_CONSUMED`), and `tests/core/log/vectors.test.ts` counts the five log files.
14. **Browser run.** The log core adds no WebCrypto primitive beyond those `e2e/core-interop.spec.ts` already proves
    in Chromium (Ed25519, SHA-256, HPKE, AES-GCM); the log vector suite runs in Vitest only. The Playwright check
    stays at the 13 crypto files.
15. **Decertification without the hub's word.** `decertificationBatch(log, platform, …)` builds the batch an admin
    owes after a platform device was decertified (`device_unassign` for each of its workgroups + R) from the verified
    platform log; WEB-06 may run it whenever it is non-null instead of relying only on `rotation_pending`.
    `pendingRotation` covers the log-derived pending scopes (self-`remove`) and any list the caller passes.
16. **`CK` in the `device_assign` batch.** 04-crypto.md section 5.9 lets the assigning browser wrap its own `CK` for
    the device, but `checkBatch` (like the Python reference and the hub) only accepts `org`/`wg` wraps in a log
    batch. The `cred` wraps go through `POST /v1/wraps` right after the batch (06-web.md section 11 job 5,
    `missingCredWraps`/`checkCredWrap`).
17. **Self-`remove` with wraps.** A leaving principal is not active after its batch, so it cannot store wraps
    (`wrap_unauthorized`); the builder never adds wraps to a leave batch and the UI must not combine a leave with
    other entries that would need them.
18. **Review findings not changed.** The crypto vector test now imports `LOG_CONSUMED` (decision 13) – a test-only
    dependency that the WCR-01 comment asked for; a discovery mechanism for two suites would add indirection without
    benefit. The property test does not generate platform devices (their paths are covered by the scenario and
    vector tests).
