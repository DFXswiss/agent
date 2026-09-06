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

Draft publication is immediate after the first signed task commit; see the
lifecycle. A draft plus local tests is not done. Ready for review is signed
commits on a branch in this repository, grok quality and logic then Codex
quality and logic on this head with zero findings, CI green on this head, then
leave-draft. Ready for review is still not merge and not completion. The
authoring session does not sit those PR reviews. A human merges; claim
completion only after that merge is verified. The local-CI comment schema for
private product repositories is `docs/local-ci-v1.md`.
