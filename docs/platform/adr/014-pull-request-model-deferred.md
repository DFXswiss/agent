# ADR-014: Pull-request data model deferred (KONZEPT §11, §14.7)

Status: Accepted (2026-11-21)

## Context
KONZEPT §11 states that a detailed relational model for pull requests and their links was deliberately postponed; §14.7 lists it as "later". PR identifiers, titles and URLs are confidential under ADR-003.

## Decision
- v1 has **no PR tables** at the hub or in the runner store.
- PR data exist only inside encrypted `operation.params` / `operation.result` events (`github.create_pull_request`, `github.get_pull_request`, `github.comment`).
- Duplicate protection for PR creation uses the dedupe tag over (repo, head, base) plus a deterministic check for an open PR with the same head/base at GitHub.
- Clients show PRs created by a task by decrypting its operation results.

## Consequences
- No cross-task PR overview in v1; a later ADR can add an encrypted PR index (scope `wg`) without schema changes at the hub.

## Alternatives considered
Plaintext PR table at the hub (violates §5); encrypted PR index events now (scope creep against the explicit deferral).

## Deviation from DESIGN.md
None (DESIGN §24.5 "pull requests are read, not owned" is compatible).
