---
name: session-store
description: >-
  Local session store for this device. Read before the first delegation and
  whenever recording sessions, tasks, rounds, checklists, or review gates:
  install and run `agent` from this repo; the store is PostgreSQL on loopback.
---

# Session store

Install this package locally and put `agent` on `PATH`. There is no second
binary.

```bash
pip install -e ".[test]"
agent init
agent session register --id <session-id> --kind human|runner|other
agent skills path
```

`agent skills path` prints the directory of the packaged review-contract files
(`spine/SKILL.md`, `review-loop/SKILL.md`, `pr-review/SKILL.md`). Those files
**are** the contract. Attach them on the session that will run the loops:

```bash
agent session skill attach --id <session-id> --skill spine
agent session skill attach --id <session-id> --skill review-loop
agent session skill attach --id <session-id> --skill pr-review
```

Data lives under `$AGENT_HOME` or, if that is unset, `~/.local/share/agent`.
Kind is `human`, `runner`, or `other`.
