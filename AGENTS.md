# Agent

Read [CONTRIBUTING.md](CONTRIBUTING.md), [DESIGN.md](DESIGN.md), and
[docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md) before
changing this repository. Skill contracts live next to the client:
`agent skills path` (spine, review-loop, pr-review, error-fix).

Static scripts own assignment acceptance, its issue confirmation before the
implementation lane starts, every lane/subagent start, test execution, and all
GitHub communication. Implementers and reviewers never run tests, spawn agents,
or access GitHub themselves. See DESIGN.md §§19.1 and 19.7. Distinguish required
behavior from implemented and verified behavior; never invent evidence.

Models never start monitors, poll status, or wait for CI or other events. Return
results or blockers to the script when there is no more work. The script owns
monitoring and informs a model when an observed event provides useful work.

Installation defaults for GitHub accounts, AI accounts, roles, and selections
are unconfigured (`NULL`). Add them explicitly through configuration, with no
fixed count. See DESIGN.md §19.8 and docs/github-accounts.md for the implemented
GitHub configuration, and docs/ai-accounts.md for AI profiles, named roles and
explicit launch/usage selections. A38 visibility lookup requires a configured
GitHub session unless visibility is explicitly supplied.

The optional static issue coordinator is configured through
`$AGENT_HOME/coordinator.json`; see [docs/issue-coordinator.md](docs/issue-coordinator.md).
`agent coordinate --session ID` advances the selected worker; `--follow` is the
script-owned loop. Installation enables no worker. Legacy assignment dispatch
and `supervise` refuse a session selected for this coordinator.

Draft publication is immediate after the first signed task commit; see the
lifecycle. A draft plus local tests is not done. Ready for review is signed
commits on a branch in this repository, grok quality and logic then Codex
quality and logic on this head with zero findings, CI green on this head, then
leave-draft. Ready for review is still not merge and not completion. The
authoring session does not sit those PR reviews. A human merges; claim
completion only after that merge is verified. The local-CI comment schema for
private product repositories is `docs/local-ci-v1.md`.
