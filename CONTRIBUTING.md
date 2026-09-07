# Contributing

- Branch from `develop`. Never push to `develop` or `main`.
- Push the branch to this repository. Do not open the pull request from a personal fork.
- As soon as the first signed task commit exists, push and open a **draft** pull request immediately ([docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md)). Stay draft until **Ready for review** (below). A human merges; only then is the pull request completed.
- Sign commits with the GitHub identity that owns the commits.
- Public repository: English for commits and comments. The visible pull-request summary is an `EN:` block, optionally followed by a labeled `DE:` block.
- Do not name private repositories, internal hostnames, or internal infrastructure.
- Add or update tests in the same change.
- The static script runs `pytest` on the exact clean signed final head before Ready for review. Model lanes never execute tests themselves. Tests need PostgreSQL (`AGENT_TEST_PG` or a local `initdb`). Full pytest is not a gate for the first draft publication.
- Pytest (or any green local suite) is a **check**, not Ready for review and not completion.

## Ready for review

The static script starts all implementation and review lanes, including each
implementation pass that addresses findings. Model lanes never launch subagents
or other lanes and never interact with GitHub. All GitHub reads and writes are
script operations. See [DESIGN.md §§19.1–19.7](DESIGN.md#191-responsibility-split)
for the responsibility split and the required assignment workflow.

CI waiting and all other monitoring belong to the script. A model must not
start a monitor or poll for progress; it returns its result or blocker when its
work is exhausted. The script detects events and informs a lane when useful.

New integrations must preserve the unconfigured (`NULL`) installation default
for accounts, roles, and selections. Configuration is explicit; there is no
fallback identity or fixed account/role count. See DESIGN.md §19.8.

A draft plus local tests is not done. Do not claim the pull request is finished, done, or completed at that point — including after leave-draft. Draft timing and CI ownership while the draft is open are defined in [docs/pull-request-lifecycle.md](docs/pull-request-lifecycle.md).

Ready for review requires all of:

1. Signed commits on a branch in this repository, based on `develop`.
2. Four lane verdicts on **this** head, two vendor stages: grok quality and grok logic in parallel, then Codex quality and Codex logic. Quality/conformance reads this file first. The session that authored the diff does not sit those reviews.
3. Codex runs only if both grok dimensions are approved. If a vendor cannot run, abort loudly; do not record `approved`; do not substitute another vendor.
4. Zero findings only after an explicit complete pass. Empty, partial, timeout, or unavailable output is not zero findings. Iterate until all four lane verdicts on this head are approved.
5. Inner implement/review rounds (`review-loop`) are not the PR reviews (`pr-review`).
6. CI green on **this** head. This public repository uses GitHub Actions. `skipped` and `cancelled` are not green unless the workflow documents that skip. The local-CI comment schema for **private** product repositories is defined in [docs/local-ci-v1.md](docs/local-ci-v1.md) and verified by `agent local-ci verify`.
7. Stay draft until the reviews and CI above hold on this head. Then one comment whose review-pass count is those four `approved` verdicts on this head, then mark the GitHub pull request ready for review (`isDraft=false`). When spine and pr-review are attached, `agent allow --action pr-ready` only checks task state (`pushing` or `pr-review`); it is not the leave-draft verdict. Do not mark ready if it denies. Ready for review is still not merge and not completion.
8. A human merges. Claim pull-request completion only after that merge is verified. When spine is attached, `agent allow --action task-done` still needs the workflow checklist and both summary sentences; that ledger state is not proof of pull-request completion.

The AI inserts `pr.open` / `comment.post`; a rejected review gate inserts `review.post`. `agent github pending` performs GitHub HTTP. A retry reuses the existing draft.

## Pull request text

Title: the first eight characters of the session id, then ` - `, then the title.

Visible summary: at most four sentences of English, then at most four sentences of German, labeled `EN:` / `DE:` on their own lines. Details go in `<details>`.

Commit messages: a short English sentence ending with a period. No session-id prefix on commits. No force-push except rebasing an unmerged feature branch onto its current base. Do not squash the feature branch.
