---
name: review-loop
description: >-
  Inner implement/review rounds until the reviewer approves or the implementer
  is blocked. Requires spine. Reviewer is read-only.
---

# Review loop

Requires **spine**. Roles: `implementer`, `reviewer`. Reviewer vendor: `grok`. Implementer vendor: `grok|codex`.

Without this skill, implementer and reviewer `agent agent` commands refuse.

## Loop

The static script owns this loop: it starts the implementer, starts the reviewer,
and starts the implementer again for improvements. The commands below are script
operations, not instructions for a model to launch another lane. Neither role
may start subagents, execute tests, or access GitHub. See
[DESIGN.md §19.1](../../../../DESIGN.md#191-responsibility-split).

Neither lane starts monitors or waits/polls for tests, CI, or another lane.
Return the implementation result, findings, or blocker to the script. It owns
monitoring and informs the model when an event provides useful work.

No round cap. Repeat until the reviewer sets `approved` or the implementer is
`blocked`.

```bash
agent round start --task <uuid>
agent agent start --session <session-id> --task <uuid> --role implementer --vendor grok|codex --round N
agent agent finish --id <implementer-uuid> --verdict done
agent agent start --session <session-id> --task <uuid> --role reviewer --vendor grok --round N
agent agent finish --id <reviewer-uuid> --verdict approved|rejected
```

`round start` only from `open|implementing|reviewing|failed`. Refused from
`pr-review`, `gate-blocked`, and while a latest gate is `unavailable`.

- Implementer `blocked` → task `failed`. Stop.
- Reviewer `rejected` → new round (`agent round start`).
- Reviewer `approved` → close `reviewer_approved` and continue the spine.

The implementer searches the repository for an existing helper, utility, base
class, configuration key, fixture, or nearby feature that already does the job
before writing a new one. The job is the purpose the caller already has;
behavior that differs does not make a different job. For a new endpoint, a
user-interface control, a visible error text, a clock or time window, or a
permission check, the implementer reuses or extends what the search finds rather
than writing a second one: one of those goes in only when no existing element in
this repository does that job. For a helper the caller does not see, reuse is
the better change and not a requirement. When the base revision has `REVIEW.md`,
the `REUSED:` / `ADDED:` lines report that search, and an `ADDED:` line for one
of those named kinds that passes over an existing element names that element
and the different job it has; the script posts those lines into the pull
request body.

The reviewer is read-only: no tests, builds, or servers.

Empty, partial, timeout, or unavailable review output is not zero findings.
Zero findings only after an explicit complete pass.

This inner loop is not the pull-request review. `reviewer_approved` does not
close `grok_pr_*` or `codex_pr_*`. A draft plus local tests is not done.

When the ledger task is created after the inner loop, close the inner-loop keys
as `ja` with evidence from `local-check|pushing|pr-review|gate-blocked` without
starting a new inner-loop agent and without moving state back to `reviewing`.
Do not use `n_a`. Workflow `implement` closes `implementer_done` then
`reviewer_approved`. Workflow `resolve-conflicts` closes `conflicts_resolved`
then `reviewer_approved` — it has no `implementer_done` key.

Locate these files with `agent skills path`.
