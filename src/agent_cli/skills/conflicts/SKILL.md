---
name: conflicts
description: >-
  Opt-in script-owned conflict resolution for one pull request comment that
  mentions the agent and says conflicts. The script owns Git, checks, GitHub,
  and lane starts; a human merges.
---

# Conflicts

Attach **conflicts** to the runner session on the execution device:

```bash
agent session skill attach --id <session-id> --skill conflicts
```

The worker is enabled only by an explicit `$AGENT_HOME/conflicts.json` entry.
Nothing is installed or enabled by default.

## Comment mandate

A human writes exactly one pull-request issue comment whose normalized body is
`@<worker-login> conflicts`. The author must be a configured login and a GitHub
user, the comment must be unedited, and the pull request must be open. A longer
text that merely contains the phrase is not a mandate.

The static script reacts, reads the pull request, prepares an isolated worktree,
rebases the feature branch onto its base, and starts an implementer lane only
when Git reports a `UU` or `AA` regular-text conflict. The lane receives only
the conflicted paths, the replayed commit subject, the base branch, and fixed
rules. The script verifies every result, runs the configured checks, pushes with
`--force-with-lease` pinned to the observed head, and posts one marked result
or blocker comment. It never merges and never changes draft state.

The script owns the loop. Models do not search, run Git or GitHub, execute
tests, start lanes, monitor, or decide transitions. See
[DESIGN.md §19.1](../../../../DESIGN.md#191-responsibility-split) and
[docs/conflict-mandate.md](../../../../docs/conflict-mandate.md).

```bash
agent conflicts --session <session-id> --once
agent conflicts --session <session-id> --follow
agent conflicts --session <session-id> --dry-run
```

This file ships in the packaged tree. An `AGENT_SKILLS_DIR` override may omit
it, same as `error-fix`.
