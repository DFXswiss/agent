# Contributing

- Branch from `develop`. Never push to `develop` or `main`.
- Push the branch to this repository. Do not open the pull request from a personal fork.
- Open a draft pull request. Stay draft until the pull request is **done** (below). A human merges.
- Sign commits with the GitHub identity that owns the commits.
- Public repository: English for commits and comments. The visible pull-request summary is an `EN:` block, optionally followed by a labeled `DE:` block.
- Do not name private repositories, internal hostnames, or internal infrastructure.
- Add or update tests in the same change.
- Run `pytest` before you push. Tests need PostgreSQL (`AGENT_TEST_PG` or a local `initdb`).
- Pytest (or any green local suite) is a **check**, not done.

## Pull request done

A draft plus local tests is not done. Do not claim the pull request is finished at that point.

Done is all of:

1. Signed commits on a branch in this repository, based on `develop`.
2. Two independent PR reviews on **this** head, in two dimensions: quality/conformance (read this file first) and logic. The session that authored the diff does not sit those reviews.
3. Vendor order: **grok** both dimensions (in parallel), then **codex** both dimensions. Codex runs only if both grok dimensions are approved. If a vendor cannot run, abort loudly; do not record `approved`; do not substitute another vendor.
4. Zero findings only after an explicit complete pass. Empty, partial, timeout, or unavailable output is not zero findings. Iterate until both dimensions of both vendors report zero findings on this head.
5. Inner implement/review rounds (`review-loop`) are not the PR reviews (`pr-review`).
6. CI green on **this** head. `skipped` and `cancelled` are not green unless the workflow documents that skip.
7. Stay draft until the reviews and CI above hold on this head. Then one comment with the review-pass count, then mark the GitHub pull request ready. When spine and pr-review are attached, `agent allow --action pr-ready` only checks task state (`pushing` or `pr-review`); it is not the leave-draft verdict. Do not mark ready if it denies.
8. A human merges. When spine is attached, `agent allow --action task-done` still needs the workflow checklist and both summary sentences.

The AI inserts `pr.open` / `comment.post`. `agent github pending` performs GitHub HTTP. A retry reuses the existing draft.

## Pull request text

Title: the first eight characters of the session id, then ` - `, then the title.

Visible summary: at most four sentences of English, then at most four sentences of German, labeled `EN:` / `DE:` on their own lines. Details go in `<details>`.

Commit messages: a short English sentence ending with a period. No session-id prefix on commits. No force-push except rebasing an unmerged feature branch onto its current base. Do not squash the feature branch.
