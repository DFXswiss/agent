---
name: session-store
description: >-
  Local session store for this device. Read before the first delegation and
  whenever recording sessions, tasks, rounds, checklists, or review gates:
  install and run `agent` from this repo; the store is PostgreSQL on loopback.
---

# Session store

Install this package locally and put `agent` on `PATH`. There is no second
binary and no bundled cluster in a team plugin.

```bash
pip install -e ".[test]"
agent init
agent session register --id <session-id> --kind human|runner|other
```

Data lives under `AGENT_HOME` (otherwise `~/.local/share/agent`). Kind is
`human`, `runner`, or `other`.

With skill `spine` attached:

```bash
agent session skill attach --id <session-id> --skill spine
agent task create --session <session-id> --workflow implement --title "…"
agent next --task <uuid>
agent close-step --task <uuid> --key session_registered --source script --evidence "session register"
agent allow --action claim-done|pr-ready|pr-create|task-done [--session ID] [--task <uuid>] [--draft true|false] [--json]
agent run --task <uuid> [--dry-run]
```

`allow` exits 0 when permitted, 2 when denied, 1 on usage errors. Session
defaults to `GROK_SESSION_ID`. `pr-create` only checks `--draft`. `claim-done`
with no session and no open tasks allows (nothing to block). `next` /
`close-step` / `run` are the spine: one open step at a time; `close-step`
applies chain guards, then writes via `checklist set`.

Without `spine`, task/checklist/round/work/check/`next`/`close-step`/`run` refuse.
`allow` uses `spine` when it loads a session or task.
