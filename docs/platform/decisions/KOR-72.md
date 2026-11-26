# KOR-72 – Working modes: inventory, target and package plan (E-32)

Owner rule [E-32](../KONZEPT.md#e-32): nothing runs on the user's computer except at most one Claude session that
steers agent.dfx.swiss; every local building block becomes a selectable way of working, never forced and never on
by default. The package delivers analysis and design only (no product code): [ADR-098](../adr/098-working-modes.md),
the interpretation [A19](../KONZEPT.md#a-19), this note and an internal inventory with the package plan.

1. **E-32 travels with this package.** The rule was recorded by a separate documentation package whose branch was
   not on the base when this package started. Its commit is taken unchanged into this branch so the ADR's link to
   `#e-32` resolves; a rebase drops it again when the documentation package lands first. This package changes only
   the rule's "Umsetzung" line (now naming ADR-098), its requirements row and adds the open deviation in part D. If
   the documentation package changes the rule's text before it lands, the rebase conflicts on the "Umsetzung" line
   and is resolved by keeping its text and this package's ADR link.
2. **A mode belongs to a task.** E-32 says "per session or task"; every session belongs to exactly one task
   ([Idee 43](../KONZEPT.md#k-idee-43)), so choosing at the start of a session chooses for its task, and all
   sessions of the task, subagents included, work under it.
3. **Basic functions are not modes (A19).** Ledger, knock, dashboard, session store and legacy hub only store, show
   or wake up; their platform counterparts exist without choice. The spine, checklists and done gates are not
   rebuilt. This is an interpretation until the owner decides otherwise, therefore in part C, not only here.
4. **The sentence is checked by a quote, in the runner; sessions have no other way.** The hub cannot read prompts;
   the runner sees its sessions' plaintext anyway. A verbatim quote of the verified prompt of the current turn is
   the smallest formal proof that a person asked ([§10 Punkt 7](../KONZEPT.md#k-10-b7), [E-25](../KONZEPT.md#e-25)).
   An agent paired inside a platform session cannot use the tool route; the orchestrator passes on only the modes its
   own task has.
5. **Dependent modes are never switched on implicitly**; the missing choice is initiated (offer in web and tool,
   `human.question` on the sentence path).
6. **Drivers read external state; no webhooks to the hub; no repository code on the device.** Repository code runs
   in test VMs, credentials stay with the driver.
7. **A38 joins through the existing comment formats.** The guard stays a GitHub workflow; the guard and the A38
   author commands stay as a separate package for authors outside the platform. In the mode the merge stays human.
8. **Notifications are content-free**, replies are links back to the task (amends ADR-020; §5 and ADR-083 hold).
9. **The merge lane follows A2** (merge commits); a rebasing train is the owner decision D9.
10. **The legacy hub is retired without data migration** (CUTOVER §§7, 9) with two added preconditions; the team
    record is not rebuilt (in practice unused: no signing keys are configured).
11. **Accesses are decided by whoever provides them**; for the operator's organization D1–D9 are open decisions of
    the owner, one by one.
12. **No second agent software** is decided here (ADR-021); the A38 slots of different vendors stay an open
    difference in part D.
