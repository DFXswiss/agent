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

Tests exist to find and document product defects. Reject the round when tests
were in scope and a product defect was left only in chat, encoded as a passing
expect, or missing an expected-fail case plus tracker row. The contract is
DESIGN.md and CONTRIBUTING.md.

Locate these files with `agent skills path`.
