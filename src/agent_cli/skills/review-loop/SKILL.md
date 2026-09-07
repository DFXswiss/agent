---
name: review-loop
description: >-
  Inner implement/review rounds until the reviewer approves or the implementer
  is blocked. Requires spine. Reviewer is read-only.
---

# Review loop

Requires **spine**. Roles: `implementer`, `reviewer`. Inner-loop vendor: `grok`.

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
agent agent start --session <session-id> --task <uuid> --role implementer --vendor grok --round N
agent agent finish --id <implementer-uuid> --verdict done
agent agent start --session <session-id> --task <uuid> --role reviewer --vendor grok --round N
agent agent finish --id <reviewer-uuid> --verdict approved|rejected
```

- Implementer `blocked` → task `failed`. Stop.
- Reviewer `rejected` → new round (`agent round start`).
- Reviewer `approved` → close `reviewer_approved` and continue the spine.

The reviewer is read-only: no tests, builds, or servers.

Empty, partial, timeout, or unavailable review output is not zero findings.
Zero findings only after an explicit complete pass.

This inner loop is not the pull-request review. `reviewer_approved` does not
close `grok_pr_*` or `codex_pr_*`. A draft plus local tests is not done.

Locate these files with `agent skills path`.
