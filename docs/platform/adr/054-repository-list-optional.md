# ADR-054: The workgroup repository list is optional

Status: Accepted (2026-11-23)

## Context
- Concept §9 limits GitHub rights to "the connected account and released repositories". The specification turned
  that into a workgroup allow-list (`settings.workgroup` `repositories`, 03-api §9) whose default was empty, and an
  empty list allowed nothing: every `github.*` operation except `github.list_repositories` ended `repo_not_allowed`
  until an admin entered each repository.
- In operation a user connected GitHub and asked a session to look at a repository; the checkout was refused
  (`repo_not_allowed`) and the repository list showed every entry with `allowed: false`.
- The owner decided (2026-11-23): no repository has to be released one by one; the agent shall be as productive as
  possible and no unnecessary restriction shall exist.

## Decision
- The list is an option, not a precondition. **Empty** (no settings event, or an event with `repositories: []`)
  allows every repository the task owner's GitHub connection reaches; `github.list_repositories` marks each of them
  `allowed: true`. **Non-empty**: exactly its repositories, as before (`repo_not_allowed` otherwise, checked before
  GitHub is asked).
- GitHub stays the boundary: the pre-check `GET /repos/{repo}` with the owner's token refuses a repository the
  account cannot reach (`repo_not_accessible`), as before. Protected branches, fast-forward pushes, draft pull
  requests, the formal checks, the dedupe and the registration (audit) of every operation with its type and
  repository are unchanged.
- The change is the runner's alone (`WorkgroupSettings.allows`, the `allowed` flag of `github.list_repositories`).
  The settings event, its contract, the hub (it never opens the sealed settings), the guest and the golden are
  unchanged, so it works with the deployed hub, web client and golden.
- A stored event that no longer opens or parses may have held a list, so it fails closed: no repository is allowed
  (`repo_not_allowed`) until an admin saves the settings again. The pre-check data of `github.list_repositories`
  carries `restricted` beside `allowed`, so the list shows this state too.
- The web client's texts for the field follow in the next hub release ("Without entries every repository of the
  GitHub connection is allowed …").

## Consequences
- A session can act on every repository the connected account reaches, including, with the operator's token of
  ADR-053, every repository of that account, without anyone releasing it. Without a list, what stands between a
  session and such a repository is only the account's own rights at GitHub, the protected branches (no commit or
  push to them), fast-forward-only pushes, pull requests as drafts by default, and the audit of every operation.
  Admins who want a narrower boundary enter a list; the operator chooses the account and its rights (ADR-053).
- A workgroup can no longer say "no repository at all" with the list; to keep a workgroup away from GitHub, its
  members do not connect GitHub (every `github.*` operation then ends `github_not_connected`).
- A workgroup that saved an explicitly empty list before this change is no longer restricted; an empty list never
  allowed anything before, so no working setup loses access.
- Concept §9 ("released repositories") is read as "repositories the connection reaches, optionally narrowed by the
  workgroup"; recorded in `spec-deviations.md`.
