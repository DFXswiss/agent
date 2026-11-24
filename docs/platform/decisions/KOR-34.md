# KOR-34 – decisions

Organization admins read every workgroup; key handling stays out of sight (concept idea 30).
Architecture: [ADR-058](../adr/058-admins-read-every-workgroup.md). Small decisions taken while building:

1. **Read, not write.** The inherited right is read only. Concept §8 gives the admin *write on the organization*
   (management, members, devices, org whiteboard); idea 30 adds *read on all workgroups*. Nothing asks for more, so
   prompts, task titles, workgroup whiteboards and settings still need an explicit `write` grant (the admin can grant
   it to itself; the entry is visible in the log).
2. **The credential scope does not inherit** (`explicit_access`): otherwise every admin's Claude/GitHub token would be
   wrapped for every device of the organization, and every demotion would force a `CK` rotation.
3. **Platform sessions do not inherit.** Runner session checks and tools keep the explicit access (owner check B2,
   harness, `list_workgroups`, `list_members`, read scope): concept §8 forbids AI sessions a blanket view into other
   workgroups, and a session runs in one workgroup. The user's own agent (KOR-24) uses the hub API and inherits.
4. **History for admins.** Catch-up gives a recipient that reads a workgroup by inheritance every epoch, not only the
   current one (an admin reads the workgroup, so its history too); a demoting browser rotates, a promoting browser
   wraps every epoch it holds.
5. **Self-demotion** is treated like leaving: its R becomes `pending` and the remaining admins/writers rotate in the
   background (the subject could not rotate after the `role` entry: `log_rights`).
6. **No reminder.** The hub no longer creates `rotation.required` notifications; the browser job rotates silently.
   The kind stays in the schema and the enum so that existing rows stay valid (rendered neutrally).
7. **Admin invitation package unchanged** (default and invited workgroups only): an intercepted code opened without
   `accept` must not reveal more keys than before; catch-up delivers after the visible `accept`.
8. **Transition rule `compatSkip` (web)**: no wrap of a `wg` key for a device by a browser whose user reads the
   workgroup only by inheritance and did not create the epoch – an older runner would ignore it while it occupies the
   slot. Removable once every enrolled device runs a version with ADR-058.
9. **Runner refusal retry**: a slot the hub refused is retried after 15 minutes as well, not only after a log change,
   so the runner deployed before the hub starts the back-fill by itself after the hub deployment.
10. **Whiteboard notifications** with "notify" reach every reader of the level, admins included (no special case).
11. **Projection**: `hub.wg_access.inherited` (migration `0011_wg_access_inherited`) with an exact back-fill of the
    active rows; `agent-hub rebuild-projections` is optional (it adds historical revoked rows). The migration number is
    the next free one on `ad5f2026-live` (after KOR-24's `0010_agent_access`); `agent-hub migrate` refuses gaps, so a
    package that lands earlier with its own `0011` (KOR-29, KOR-31 and KOR-37 carry one) makes this one `0012` at the
    rebase (a rename of the file and of `test_migration_0011.py`, nothing else).
12. **Vectors**: the main log of `log.json` removed admin carol without rotating W1, which she now reads by
    inheritance – the hub would refuse that batch. Entries 17–21 are reordered (same count and types: ungrant, remove,
    rotate W1, rotate org, rotate W0) so that the chain stays valid under the new rule; new cases for demotion,
    self-demotion, promotion, the explicit-then-inherited change, admin wraps in both directions, the credential scope
    and catch-up with history.
13. **E2E numbering**: the new scenario is E2E-45 (40 and 41 are taken by parallel packages); E2E-18 is rewritten for
    the new rule.
14. **The way out of "lost"** (review): a workgroup whose current key nobody left can deliver (old logs: the author
    left without the rotation the old rule did not owe) is rotated silently by an admin's rotation job; the text says
    that new content becomes readable shortly. A false "lost" (another admin happens to hold the key) costs one
    harmless rotation, since older epochs stay deliverable by catch-up.
15. **Traceability** (review): ADR-027 §4 lets only acceptance packages change `traceability.json`, but the gate
    refuses a spec tag the file does not list. E2E-45 is therefore registered (R-8.1, R-8.5; milestone 3, the last the
    checker knows, `MAX_MILESTONE = 3`) and the rows of `01-requirements.md` are mirrored; R-P.5 is left to the next
    acceptance package.
16. **Contract copy in agent** (review): the runner tests run against the new vectors, so the agent branch carries the
    same `contracts/` as agent-core (byte-equal, as the merge script's sync would write it).
17. **Ungrant of an admin**: the dialog says that the person keeps reading as admin (nothing is rotated then).
18. **Error texts**: key wording removed from the user-facing error catalog (`scripts/error-texts.json`); the codes
    stay.
19. **ADR number**: the ADR index must be gapless (`test_docs`), so the ADR is ADR-058, the next free number on
    `ad5f2026-live` (after ADR-057 of the workspace scanner); a package landing earlier with its own 058 makes this one
    the next number at the rebase (a rename and a search-and-replace of `ADR-058`, nothing else).
