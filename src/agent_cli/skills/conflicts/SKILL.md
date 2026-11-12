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
when Git reports a `UU` or `AA` regular-text conflict. The lane specification
contains only the conflicted paths, the replayed commit subject, the base branch,
fixed rules, and — on the single retry — the deterministic reason why the
previous attempt failed its verification, masked like every other value. These values are passed in a delimited
data section; the fixed rules declare them and the contents of every file as
data, not instructions. Unicode control and bidirectional formatting characters
are removed from the commit subject. If a value would close the data section,
the request ends in a blocker and no lane is started. The lane runs in the
worktree and can read the repository; the script confines the result: after a
completed lane run it verifies that exactly the conflicted paths changed and
that their final content passes the deterministic checks. An attempt that did not complete counts as a
failed
verification and is retried once; changes outside the conflicted paths are
restored before the retry, while edits inside the conflicted files stay for the
retry. Staging happens only after a lane run that completed and passed the
verification. Nothing is pushed without a passing verification. The script
verifies every result, runs the configured checks, pushes with
`--force-with-lease` pinned to the observed head, and posts
one marked result or blocker comment. It never merges and never changes draft
state.

The script owns the loop. The lane is given the files to edit and does not need
to search for the task. Models do not run Git or GitHub, execute tests, start
lanes, monitor, or decide transitions. See
[DESIGN.md §19.1](../../../../DESIGN.md#191-responsibility-split) and
[docs/conflict-mandate.md](../../../../docs/conflict-mandate.md).

```bash
agent conflicts --session <session-id> --once
agent conflicts --session <session-id> --follow
agent conflicts --session <session-id> --dry-run
```

This file ships in the packaged tree. An `AGENT_SKILLS_DIR` override may omit
it, same as `error-fix`.
