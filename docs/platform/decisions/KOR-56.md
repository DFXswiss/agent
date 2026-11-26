# KOR-56 – decisions

Package: release of a workgroup to DFX support (concept [idea 29](../KONZEPT.md#k-idee-29), [A12](../KONZEPT.md#a-12)).
Architecture: [ADR-096](../adr/096-support-release.md), key model KM-33.

1. **Who releases:** an admin of the organization or a user with effective `write` on the workgroup (an agent with its
   user's rights). A writer can copy everything already; requiring an admin would be an unnecessary restriction
   ([E-11](../KONZEPT.md#e-11)). Readers may not release.
2. **Keys per person, released to the team:** the release names no person; every person of the root-signed roster gets
   wraps of every epoch (catch-up by any holder). No shared support key, so a personnel change needs no new release.
3. **The role is bound to the certification** valid at the grant (design review, high): a renewed certification needs a
   new grant; a decertified person leaves the roster.
4. **Revocation always rotates** (`R = {wg}`), independent of who is support, so every verifier agrees.
5. **Role end:** the hub refuses the person at once and derives the owed rotation from its stored wraps of the current
   epoch (not a flag); residual against a hub that withholds the role end stated in ADR-096 §3 (same class as decertified
   devices; anchoring the platform head in the org log belongs to KOR-55).
6. **What support writes:** prompts into existing tasks and the workgroup whiteboard only – no task (its model access and
   connections would belong to nobody of the organization), no workgroup settings (repository list, protected branches;
   design review, medium).
7. **Names without the org key:** the releasing client seals `support.info` (org name, workgroup name, the workgroup's
   readers by ID) under the workgroup key in the release batch; `support.info` is not delivered to devices.
8. **Live channel:** stays members-only (it multiplexes the whole organization); the support view polls, every poll is a
   read in the read log.
9. **First access** is recorded in the rights check (`support_access`, one notification per person and release) – the
   one place every read passes.
10. **Agent of a support person:** the hub authorises its requests as its user's (ADR-056), so it may fetch ciphertext
    of a released workgroup, but it is no roster member: it receives no wrap and cannot author a support event. Whether
    a support person's agent should act as support is a decision of the owner ([E-04](../KONZEPT.md#e-04) has no
    exception; ADR-096 §6).
11. **Display names of support persons** are set by the operator (`grant-role support --name`, `support-name`) and kept
    in clear at the hub (operator staff, not customer content); the UI adds the ID mark so a changed name cannot hide who
    acted.
12. **Notifications:** `support.release` (admins, the releasing user, and every support person for a grant),
    `support.access` (admins and the releasing member), `support.people` (admins of orgs with an active release).
13. **Independent review after the build** (fresh context, 0 high, 5 medium, 6 low): fixed – the runner accepts only
    the current support roster as prompt authors; a member that is also support writes under the release (hub route
    and audit `support.write`); a session instructed by a support person reads its released workgroup and nothing
    else; first access is recorded only when the right is granted; ADR-096 aligned with the code (audit names, derived
    `rotation_pending`, `support.info` in the batch, residuals of history and concurrency). Kept with reason: the
    support view shows tasks and conversations, the whiteboard through the API (a later view); only the latest bound
    certification verifies history (identity keys do not change on renewal); the roster rebuild replays a small
    copy of the rule because the rebuild has no root key (`projections.roster_of`, tested against the hub's
    incremental state); live publishing of `support.access` waits for the next notification fetch.
14. **Shared lists** (ADR-027): `static_routes.SPA_ROUTES`, `agent_platform.registry`, `backup.tables.RULES`, the web
    guard and view lists and `traceability.json` have no registration hook; like earlier packages, this package adds
    one line each (append-only, the merge resolves them). E2E-72 carries milestone 3 because the traceability check
    knows milestones up to 3; the package belongs to milestone 5.
15. **Platform E2E and the lanes of the web suite:** after the rebase on `develop`, the web fixtures enter a lane of the
    web suite automatically (a worker fixture that waits for that lane's hub); a platform run has no such lane, so every
    platform spec waited 25 minutes and failed. The lane fixture of `web/e2e/fixtures/test.ts` now enters no lane in a
    platform run (`E2E_CONTROL_URL` set; one hub of its own); found with E2E-72, fixes every platform spec.
16. **Initial JavaScript:** raised to 224 KiB with its reason in 06-web §12.2 (the verifier rules and the support parts
    of the background jobs belong to the first load; screens, dialog and texts load lazily).
