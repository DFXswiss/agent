# HOT-33 – decisions (GitHub writes through the fork of an identity that may only read)

Observed: a task asked for `github.create_branch` in private repositories of an organisation where its GitHub identity
has the roles pull and triage. Reading worked (`github.list_pull_requests`, `github.get_pull_request`). The pre-check
(`reach.check_write`, KOR-65) turned `permissions.push: false` into `repo_not_accessible` with the step
`check_access`, the pipeline held the operation 15 minutes for `github_access`, and at the end the message was rebuilt
from the held step without its "write" mark, so it read "has no access to <repo> (or it does not exist)". The identity
had a fork of each repository, and earlier pull requests came from those forks.

1. **What the 15-minute hold is for.** KOR-25 (`github_connection`) and KOR-65 (`github_access`): the person who gave
   the instruction connects GitHub, installs the App, approves the OAuth App or authorises SSO at GitHub, and the
   runner then sees the new state by itself (a new credential, or the access check repeated every 30 s), so the held
   operation goes on without a new request of the session. A missing right to push is no such state: the usual way
   is the fork, and a role change by an owner of the repository is not something the session should wait for. The
   hold stays exactly for those two reasons; a read-only repository is never held.
2. **Fork by itself, for the whole class.** `github.create_branch`, `github.commit` and `github.push` route through
   `github.fork.route` after `GET /repos/{repo}`; `github.create_pull_request` through `fork.pull_route`. The session
   keeps naming the original repository (dedupe, allow-list, workspace directory `repos/<owner>/<name>` and the
   checkout stay as they are); the pre-check data carries the fork, the results carry `fork`.
3. **Finding the fork.** First `GET /repos/{login}/{name}` (one request in the common case) when it is a fork whose
   `parent` is the repository; else the repository's fork list for one owned by the identity, so a fork with another
   name is found. Forks in organisations of the identity are not searched (the identity's own account is GitHub's
   default target of a fork).
4. **Creating the fork at execution.** A fork is a write; it is created by the executor (after the hub registered the
   operation), never in the pre-check. GitHub creates forks asynchronously: the executor waits for the fork's default
   branch (1, 2, 4, 8 s) and otherwise fails retryable; a repeated `POST …/forks` answers the existing fork.
   `default_branch_only: false`, so a base branch other than the default exists in the fork as well.
5. **Bringing the fork up to date.** Before the branch is created, `merge-upstream` for the branch `from` names; the
   new branch itself starts at the commit resolved in the original repository (forks share their objects), so a fork
   branch that is missing or diverged (`404`, `409`, `422`) does not stop the operation and is left as it is.
6. **A commit before any fork.** A `github.commit` of a new branch with no fork yet takes its parent from the original
   repository (or the checkout) in the pre-check; the executor creates the fork and writes the commit there.
7. **Pull requests.** From the fork only when the identity may not push and the fork holds the head branch; a head
   that only the original repository has (made by someone with the right) stays a pull request within it.
8. **Exact refusal, own code.** `repo_read_only` (operation error code, additive, `contracts/changes/HOT-33.md`) when
   no fork can be used: the repository does not allow forks and the identity has none, GitHub refuses the fork, or the
   connection may not write the fork. The message names the account, its rights on the repository (`pull, triage`)
   and the next step (an owner gives write access or allows forks). It is never held. `repo_not_accessible` remains
   only for a repository the connection does not reach (`404`, organisation restrictions, SSO). The unused
   "check_write" sentence of `reach` is gone with it.
9. **Guide.** The session rules (`images/harness/RULES.md`) and the description of `request_operation` name the fork
   way and `repo_read_only`.
10. **Older guests.** The guest validates answers against its bundled contracts (HOT-23), so the guest version is
    0.8.0 and a guest before it (`guest_features.FORKS`) gets the answer of an operation, `get_operation`,
    `list_operations` and `list_workgroup_operations` without `fork` and with `repo_read_only` named
    `repo_not_accessible`; the message keeps the exact sentence. The hub and the events carry the real code.
