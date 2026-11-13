# Agent

Read [CONTRIBUTING.md](CONTRIBUTING.md), [DESIGN.md](DESIGN.md), and
[docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md) before
changing this repository. Skill contracts live next to the client:
`agent skills path` (spine, review-loop, pr-review, error-fix, conflicts).

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
and `supervise` refuse a session selected for this coordinator. For the target
repositories of an explicitly configured A38 coordinator worker, the guard alone
owns Ready and the second review comes from configured team members through
the hub, or from model slot `second` when the worker sets
`a38_second_review: "model"`.

Orders are submitted and answered only through `agent order` or the orders view
of the local dashboard. Models never write `order.event` or `order.reply` and
never close a precondition check; an override names its check. A model that
hands orders to the script is a concierge, not an orchestrator: it submits,
reads status and the acceptance report, relays questions, and passes on the
human's reply, override or cancel. It starts no lane, runs no review loop and
does not wait with model turns.

Draft publication is immediate after the first signed task commit; see the
lifecycle. A draft plus local tests is not done. Ready for review is signed
commits on a branch in this repository, model slot `first` quality and logic
then model slot `second` quality and logic (providers pinned on the task from `ai-accounts.json`) with zero findings on a head that is the current head, or a commit after the pull request base that the current head still contains, CI green on this head, then
leave-draft. Pull requests to this repository take their second review from
model slot `second`, not from the A38 external review; the `one_review_authors`
exception of CONTRIBUTING.md still applies. Ready for review is still not merge
and not completion. The
authoring session does not sit those PR reviews. A human merges; claim
completion only after that merge is verified. The local-CI comment schema for
private product repositories is `docs/local-ci-v1.md`.

An optional implementation provider is pinned separately on each task; when it
is absent, implementation uses model slot `first`. The two PR review pairs
remain model slots `first` and `second`.
