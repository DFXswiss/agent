---
name: spine
description: >-
  Task spine: checklist, rounds, local checks, open work, and the one-step
  chain (allow / next / close-step / run). Attach before those commands.
---

# Spine

A session must exist. Attach this skill before creating tasks:

```bash
agent session register --id <session-id> --kind human|runner|other --skill spine
# or later:
agent session skill attach --id <session-id> --skill spine
```

Without spine, `task`, `checklist`, `round`, `check`, `work`, `allow`, `next`,
`close-step`, and `run` refuse.

## One open step

`agent next`, `agent close-step`, and `agent run` are the spine. Exactly one
step is open. Do not skip keys. `close-step` applies chain guards, then writes
via `checklist set`.

```bash
agent task create --session <session-id> --workflow implement|review|resolve-conflicts --title "…"
agent next --task <uuid>
agent close-step --task <uuid> --key session_registered --source script --evidence "session register"
agent run --task <uuid> [--dry-run]
agent allow --action claim-done|pr-ready|pr-create|task-done [--session ID] [--task <uuid>] [--draft true|false] [--json]
```

`allow` exits 0 when permitted, 2 when denied, 1 on usage errors.

## Checklist values

Keys are `pending`, `ja`, `nein`, or `n_a`. `ja` and `n_a` need `--evidence`.
`unavailable` is not `n_a`; it blocks `done`.

## Workflow keys

Do not invent keys. Chains:

**implement:** `session_registered`, `spec_written`, `implementer_done`,
`reviewer_approved`, `local_check_pass`, `pushed`, `grok_pr_quality`,
`grok_pr_logic`, `codex_pr_quality`, `codex_pr_logic`, `contributing_ok`,
`deviation_declared`, `deviation_granted`

**review:** `session_registered`, `contributing_read`, `grok_pr_quality`,
`grok_pr_logic`, `codex_pr_quality`, `codex_pr_logic`, `coverage_ok`,
`handbook_ok`, `contributing_ok`, `deviation_declared`, `deviation_granted`

**resolve-conflicts:** `session_registered`, `conflicts_resolved`,
`reviewer_approved`, `local_check_pass`, `pushed`, `grok_pr_quality`,
`grok_pr_logic`, `codex_pr_quality`, `codex_pr_logic`, `mergeable`

`done` still requires the workflow checklist and both summary sentences
(`agent task summary`).

Locate these files with `agent skills path`.
