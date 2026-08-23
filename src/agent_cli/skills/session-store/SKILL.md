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

`agent skills path` prints the packaged skill contracts
(`spine/SKILL.md`, `review-loop/SKILL.md`, `pr-review/SKILL.md`), or
`AGENT_SKILLS_DIR` when that is set. Those three files **are** required.
`error-fix/SKILL.md` ships in the packaged tree; an override need not copy
it. Unset `AGENT_SKILLS_DIR` and run `agent skills path` again to print
the packaged directory. For implement/review, attach the review trio. For
production-error → draft PR, attach all four (`spine`, `review-loop`,
`pr-review`, `error-fix`) on the **runner** session:

```bash
agent session skill attach --id <session-id> --skill spine
agent session skill attach --id <session-id> --skill review-loop
agent session skill attach --id <session-id> --skill pr-review
agent session skill attach --id <session-id> --skill error-fix
```

Data lives under `$AGENT_HOME` or, if that is unset, `~/.local/share/agent`.
Kind is `human`, `runner`, or `other`.
