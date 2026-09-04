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
agent agent finish --id <reviewer-uuid> --verdict approved|rejected|unavailable
```

- Implementer `blocked` → task `failed`. Stop.
- Reviewer `rejected` → new round (`agent round start`).
- Reviewer `approved` → close `reviewer_approved` and continue the spine.
- Either role `--verdict unavailable` → vendor CLI unreachable: neutral release
  that clears the `working` agent row with no task/round state change, so a
  later retry is unblocked.

The reviewer is read-only: no tests, builds, or servers.

Empty, partial, timeout, or unavailable review output is not zero findings.
Auto-pass only when the lane output is a single terminal report with
`STATUS: complete` and an explicit, present `FINDINGS:` header that parses to
zero -- a missing or duplicated `FINDINGS:` header, or multiple STATUS:/FINDINGS:
blocks in the output, resolves to retry instead. A zero-`FINDINGS:` report still
resolves to retry, not an automatic pass, when its `GAPS:` section discloses
genuine, non-trivial content (anything other than a zero token like `none`/`0`),
or when the output contains more than one `GAPS:` header.

This inner loop is not the pull-request review. `reviewer_approved` does not
close `grok_pr_*` or `codex_pr_*`. A draft plus local tests is not done.

Locate these files with `agent skills path`.
