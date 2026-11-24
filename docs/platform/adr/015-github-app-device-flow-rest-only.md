# ADR-015: GitHub App, device flow, REST only (KONZEPT §9)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §9: users connect and disconnect their GitHub account; repository access, branches, commits, pushes, PRs and comments; rights limited to the connected account and released repositories; everything audited; recommendation to evaluate a GitHub App with granular permissions; Claude Code never runs git.
- Legacy: OAuth App with scope `read:user repo` stored in plaintext at the hub; `agent` executes `git`/`gh` with per-account config directories (`docs/github-accounts.md`).
- The hub must not see tokens (ADR-003); the browser cannot call GitHub OAuth endpoints (no CORS); runners are not reachable from GitHub (no callback).

## Decision
- A **GitHub App** with repository permissions contents (read/write), pull requests (read/write), issues (read/write), metadata (read); **device flow enabled**; **user-to-server token expiration disabled** in the App settings (refresh would require the client secret on every device).
- Connect = device request `github.connect` to an eligible device; the runner runs the device flow with the public client ID, relays `user_code`/`verification_uri` to the user, polls, stores the token as `github` credential under the user's CK. Disconnect = hard delete of the record + cache purge + link to revoke the grant at GitHub.
- Execution on the runner host via **REST and Git Data API only**: tarball download for checkout, blobs/trees/commits for commit, ref update (fast-forward) for push. No `git` binary, so no hooks/filters of untrusted repositories run.
- Released repositories = intersection of App installation selection and the workgroup `repositories` allow-list (an empty list restricts nothing: ADR-054); protected-branch patterns; PRs draft by default; formal rules in `03-api.md` §9.
- Operations use the credential of the **task owner**.

## Consequences
- Least privilege per repository; tokens exist only on execution devices.
- Non-expiring tokens rely on explicit disconnect/uninstall for revocation (documented in the UI).
- GitHub double must implement device flow, REST and Git Data API (`agent_doubles.github`).

## Addendum (2026-11-23)
The device flow stays unchanged. By the owner's decision a second, equal way exists: an existing GitHub token (GitHub
CLI login, personal access token) checked by the execution device and stored as the same credential
([ADR-065](065-github-existing-token.md)); the "pasted PATs" alternative below is therefore no longer excluded. The
App's repository permissions stay Contents, Pull requests and Issues "Read and write" and Metadata "Read-only".

## Addendum (2026-11-24, KOR-65)
By rule E-29 "Connect GitHub" signs in with an **OAuth App** first (scopes `repo workflow`, device flow without a
secret), because a GitHub App user token reaches only owners where the app is installed; the GitHub App stays as a
second way and an access refusal is held with one next step ([ADR-077](077-github-every-repository.md)).

## Alternatives considered
OAuth App with `repo` scope (too broad), web flow via hub callback (token at hub), `git` CLI on host (executes repository-controlled config), fine-grained PATs pasted by users (poor UX, manual expiry).

## Deviation from DESIGN.md
Yes: §19.1 / `docs/github-accounts.md` (gh CLI with operator-configured accounts and signing keys); PRs remain drafts and merges stay human, as in DESIGN §19.
