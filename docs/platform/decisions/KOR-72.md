# KOR-72 – Working modes: inventory, target and package plan (E-32)

Owner rule [E-32](../KONZEPT.md#e-32): nothing runs on the user's computer except at most one Claude session that
steers agent.dfx.swiss; every local building block becomes a selectable way of working, never forced and never on
by default. The package delivers analysis and design only (no product code): [ADR-098](../adr/098-working-modes.md),
this note and an internal inventory with the package plan.

1. **E-32 travels with this package.** The rule was recorded by a separate documentation package whose branch was
   not on the base when this package started. Its commit is taken unchanged into this branch so the ADR's link to
   `#e-32` resolves; a rebase drops it again when the documentation package lands first. This package only changes
   the rule's "Umsetzung" line (now naming ADR-098), its requirements row and adds the open deviation in part D.
2. **A mode belongs to a task.** E-32 says "per session or task"; every session belongs to exactly one task
   ([Idee 43](../KONZEPT.md#k-idee-43)), so choosing at the start of a session chooses for its task, and all
   sessions of the task, subagents included, work under it. A per-subagent choice would let a child do more than its
   task, which ADR-094 rules out for rights and this ADR rules out for behaviour.
3. **The sentence is checked by a quote, in the runner.** The hub cannot read prompts (zero knowledge); the runner
   sees its sessions' plaintext anyway. A verbatim quote of the verified prompt of the current turn is the smallest
   formal proof that a person asked ([§10 Punkt 7](../KONZEPT.md#k-10-b7), [E-25](../KONZEPT.md#e-25)) and keeps
   text from files, comments or mails from switching a mode on. Switching off needs no quote.
4. **Dependent modes are never switched on implicitly.** The refusal names the other mode and the web and the tool
   offer to choose both in one step ([E-05](../KONZEPT.md#e-05)).
5. **Drivers read external state; no webhooks to the hub.** Same pattern as the fresh pull-request read of ADR-084;
   webhook bodies would be plaintext content at the hub.
6. **A38 joins through the existing comment formats.** The guard of a repository that uses A38 is a GitHub workflow,
   not a local program; the mode writes the report and review comments the guard already accepts, so no adopting
   repository changes anything, and the guard code stays in this repository after the legacy tool's commands go.
7. **No rebuild of the legacy ledger.** Checklists, rounds, the spine and done gates of the legacy tool are replaced
   by operations, `review.verdict`, merge criteria (ADR-094) and the drivers of the modes; there is no data
   migration (CUTOVER §9).
8. **Merge stays human in the mode `a38`.** ADR-094 lets a session with the role `merge` merge under criteria; A38
   requires a person's merge, so the mode never requests `github.merge` unless a person asks for it.
9. **Accesses are listed, not assumed.** ADR-098 §7 names D1–D8 (Apple signing, production hosts, production logs,
   mailboxes, Telegram, further subscriptions and vendors, GitHub accounts beyond the user's own, which Macs serve
   checks); each is a decision of the owner, one by one.
