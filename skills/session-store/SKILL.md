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
agent allow --action claim-done|pr-ready|pr-create|task-done [--session ID] [--json]
agent run --task <uuid> [--dry-run]
```

`allow` exits 0 when permitted, 2 when denied, 1 on usage errors. Session
defaults to `GROK_SESSION_ID`. `next` / `close-step` / `run` are the spine:
one open step at a time; `ja` only through `close-step` (or `checklist set`
with the same guards).

Without `spine`, task/checklist/round/work/check/`allow`/`next`/`close-step`/`run` refuse.
