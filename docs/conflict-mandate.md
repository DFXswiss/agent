# Comment mandate: conflicts

A human can authorize conflict resolution for one pull request with one
pull-request comment. The static script on the execution device finds the
comment, verifies the mandate, rebases the feature branch onto its base, starts
a confined model lane only for conflicted files, verifies the result, runs the
configured checks, pushes the rebased signed branch, and reports in the pull
request. The model never searches, touches Git or GitHub, runs tests, or decides
a transition. When Git rebases without a conflict, no model lane starts.

The responsibility boundary is [DESIGN.md §19.1](../DESIGN.md#191-responsibility-split)
and [§19.9](../DESIGN.md#199-comment-mandate-conflicts). The feature is an
opt-in skill named `conflicts`; installation enables no worker.

## Implemented versus deployed

| Piece | Status |
|---|---|
| Configuration schema (`conflict_mandate_config.py`, `$AGENT_HOME/conflicts.json`) | Implemented. Absent, `{}`, or null/empty `workers` enables **no** worker. |
| Runtime (`conflict_mandate.py`, `conflict_mandate_git.py`, `conflict_mandate_github.py`) | Implemented in this repository: discovery, mandate check, isolated worktree, rebase loop, lane verification, checks, push, and comments. |
| CLI | `agent conflicts --session ID` runs one tick; `--follow` is the script-owned loop; `--dry-run` is read-only. |
| GitHub and Git accounts | Taken only from the worker session's explicit `github-accounts.json` binding through the existing account runner. No ambient login or fallback. |
| Model provider | The device implementer selection from `ai-accounts.json`. Missing configuration or `codex` starts no lane. The binding must be `workspace-write`. |
| Operator workspace, repositories, logins, checks, and timeouts | **Never installed automatically.** The operator adds them explicitly. |
| End-to-end deployment on a named host | Not claimed. Deployment hostnames stay out of this public repository. |
| Universal sandbox / forced model isolation | **Not claimed.** The lane provider's process argv is the available hardening, not a universal sandbox. |

This document does not invent evidence that a device runs this worker.

## Explicit configuration

`$AGENT_HOME/conflicts.json` starts absent. Every worker entry requires all of
these fields; unknown keys and default selections are rejected.

```json
{
  "workers": {
    "worker-session": {
      "workspace_root": "/absolute/operator/worktrees",
      "repositories": {
        "example/project": {
          "check_argv": ["/absolute/operator/full-checks"]
        }
      },
      "request_logins": ["AuthorizedHuman"],
      "poll_seconds": 60,
      "lane_timeout": 1800,
      "check_timeout": 900,
      "max_rebase_steps": 50
    }
  }
}
```

- `workspace_root` is an absolute path. State, checkpoints, lane files, and
  worktrees are namespaced by the worker session id:
  `<workspace_root>/.conflicts-control/<session-id>/...` and
  `<workspace_root>/<session-id>/<owner>__<name>/...`. A session id must be a
  safe single path component (`A-Za-z0-9_-`). Workers sharing one
  `workspace_root` therefore never read or touch each other's requests.
- `repositories` names configured `owner/name` repositories and their trusted
  `check_argv`. An empty command list is rejected.
- `request_logins` is a non-empty list of GitHub logins allowed to issue the
  mandate.
- `poll_seconds` is at least 30, `lane_timeout` and `check_timeout` are at least
  60, and `max_rebase_steps` is between 1 and 200.

GitHub and Git use the worker session's configured account and require a git
identity with signing configuration. The private key and trusted signer setup
stay in that account executor, never in this file. The model lane uses the
device's implementer selection; `codex` is refused because it can run commands.

## Mandate

A request is one pull-request issue comment in a configured repository where:

1. The comment belongs to an open pull request.
2. Its author is a GitHub `User` whose login is in `request_logins`.
3. The body, after trimming and collapsing whitespace runs, equals
   `@<worker-login> conflicts`, case-insensitively.
4. `created_at == updated_at`, so edited comments are ignored.
5. No result marker for that comment exists yet.

Discovery reads comments oldest first with the worker account across all
configured repositories, bounded to the last 24 hours per repository on the
first scan and then from a persisted checkpoint minus a small overlap. The
oldest qualifying request is handled in a tick. A marker for another request
does not block a newer one, and requests for the same pull request are
serialized by the source lock.

## Workflow

1. The script adds the worker's `eyes` reaction.
2. It reads the pull request and retries a bounded number of times when
   `mergeable` is unknown. Unknown remains a later tick, not a blocker.
3. It verifies the push route. A fork owned by the worker is accepted. A
   same-repository branch requires `permissions.push`. A protected head ref is
   refused. Other forks and protected refs receive one blocker comment. The
   head ref is validated directly after the pull-request read and before any
   clone with Git's documented branch rules (no control characters, space,
   `~`, `^`, `:`, `?`, `*`, `[`, `\`; no `..`, `@{`, `//`; no leading `/`,
   `-`, or `.`; no component starting with `.` or ending with `.lock`; no
   trailing `/` or `.`; not `@`). Valid names such as `feature@2` and non-ASCII
   names pass. The same validator applies to the base ref and to the lease push.
   Anything else finishes with the blocker `unsupported head branch name`.
4. If GitHub reports `mergeable == true`, it posts a no-conflict result and does
   not clone or start a lane.
5. Otherwise it creates a fresh worktree, sets the head repository as
   `publication` and the target repository as `origin`, fetches both branches,
   and refuses to continue if the fetched head differs from the observed head.
   The clone uses `--separate-git-dir` with a Git directory in the request's
   control directory, so the worktree contains only a regular `.git` file. The
   script records that file's exact bytes after the clone and refreshes the
   snapshot whenever a rebase stop precedes a lane run. It verifies that
   `.git` is still a regular file with the expected bytes once directly after
   each lane run, before the first Git command of the verification, and again
   between the checks and the push. A change removes the worktree
   and finishes with the blocker `lane changed the repository metadata`
   without running Git in it. Local Git commands of this worker pass
   `-c core.hooksPath=/dev/null`.
   Network `clone`, `fetch`, `push`, and `ls-remote` commands pass no global
   `-c` override. Directly after the clone writes `core.hooksPath=/dev/null`
   into the repository configuration with a local command, so later transfers
   use the same setting from the separate Git directory outside the worktree.
6. It refuses merge commits. It rebases the feature branch onto
   `origin/<base>` with the account's signed-commit configuration.
7. For only `UU` and `AA` regular text conflicts, it writes a lane spec outside
   the worktree, starts the configured implementer lane, and verifies the
   change set, conflict markers, and `git diff --check`. The allowed change set
   is measured against the index: the union of `git diff --name-only -z`,
   `git ls-files --others -z` including ignored files, and the unmerged paths
   must stay within the conflicted paths. Files that Git staged cleanly while
   replaying the commit are equal in the worktree and the index and are not
   reported. The same measurement runs immediately before the lane; a path
   outside the conflicted set there finishes with the blocker
   `unexpected worktree state before the lane`. The base branch and commit
   subject are redacted before they enter the spec. Unsupported head or base
   branch names finish the request with a blocker before any clone. A failed
   first result is retried once with the exact failure in the spec. Other
   unmerged states, a second failure, a blocked lane result, or too many stops
   abort the rebase and post one blocker.
8. It verifies the clean signed head, base ancestry, and every commit in
   `origin/<base>..HEAD`, then runs `check_argv` with a bounded timeout. After
   the checks it recomputes that range, requires the same commit list and the
   same `origin/<base>` sha as before the checks, and verifies every commit in
   the recomputed range as signed. A failing check's redacted output tail is
   bounded before publication. The check child inherits the device environment
   without the ambient GitHub token variables and with an empty unconfigured
   GitHub profile, plus the
   `AGENT_CONFLICTS_HEAD`, `AGENT_CONFLICTS_BASE`, `AGENT_CONFLICTS_REPO`,
   `AGENT_CONFLICTS_PR`, `AGENT_CONFLICTS_SESSION`, and
   `AGENT_CONFLICTS_WORKTREE` variables.
9. It pushes only with
   `--force-with-lease=refs/heads/<head-ref>:<old-head>`. Before the push it
   verifies again that `HEAD` equals the verified sha, the worktree is clean by
   the index measurement and `git status`, the `.git` file is unchanged, every
   commit of the range still verifies as signed, and the `origin` and
   `publication` URLs are the expected repositories. A difference finishes with
   the blocker `checks changed the repository state` without a push. It also
   persists the old head, new head, replayed commit count, resolved files, and
   check result with phase `pushing`; after a successful push it persists phase
   `result-pending`, re-reads the pull request with the bounded `mergeable`
   retries, and posts the result once from the stored pre-push facts plus that
   fresh value. The `Mergeable` fact is `true`, `false`, or `unknown` when the
   value is still null. A pull-request read that fails after the push still
   posts the result with `Mergeable: unknown`. A resumed `pushing` request
   whose pull-request head equals the stored new head, and a resumed
   `result-pending` request, do the same read before posting rather than
   posting a body rendered before the push.

`--dry-run` performs discovery and pull-request reads and prints
`would accept ...`, `would rebase <head> onto <base>`, and the push route. A
protected head ref or a refused route prints `would post blocker: ...` instead
   of a push route. An invalid head or base ref and the protected-branch check
   are reported before the mergeability value is considered. When GitHub still
   reports an unknown `mergeable` value it prints the same unknown-state
   observation as a normal run and no `would rebase` line. Dry-run does not
   react, clone, start a lane, run checks, push, comment, or write state.

## Comments and idempotency

Result and blocker comments begin with
`<!-- agent-conflicts:result:v1:<comment-id> -->`. Before posting, the script
lists pull-request comments by the worker login and reuses an existing marker.
A request whose marker exists is finished whatever the local state says.

Comments use a short `EN:` block, a short `DE:` block, and a facts
`<details>` block. All dynamic text passes through `redact`; local paths never
appear.

## Local state and failure classes

The session control directory holds lane specs, lane logs, the separate Git
directory, and `state.json`; the session checkpoint holds the newest processed
comment timestamp per repository. State
writes are atomic and are only an optimization: GitHub markers and the live
pull request are the source of truth. Lane runs are recorded through
`lane.run`; there is no new table or activity type.

Every blocker aborts an in-progress rebase, pushes nothing, posts exactly one
blocker, and finishes that request. A new comment is needed to try again.
Unreadable GitHub or network state, unknown `mergeable`, and a rejected push
lease post nothing and retry later from the pull-request read. After a failed
push, the worker reads `refs/heads/<head-ref>` through `git ls-remote
publication`. If that read fails, the failure is transient. If the remote head
differs from the observed old head for this request, the lease was rejected,
so it is transient and the next tick starts from the pull-request read. If the
remote head still equals the observed old head, the push was refused for
another reason (authentication, hook, or policy): the request finishes with a
blocker comment saying `push was refused by the remote` and includes the
masked, bounded tail of the push output. After 24 hours
of transient failures in any other phase, including `result-pending` and a
failing marker lookup before resumption, they
finish with a blocker. A blocker comment that cannot be posted remains in
`blocker-pending` and is retried on every tick without a time limit; the
24-hour limit applies to every other transient failure. Aborting a rebase is a
local Git operation and uses the request's already-created Git runner; it does
not authenticate to GitHub again. A stored request is resumed only when both
its recorded worker session id and worker login equal the current worker's;
anything else is ignored. The checkpoint advances when a request finishes; an
accepted unfinished request resumes from its control state rather than
replaying an older mandate. A comment whose result marker already exists is
not a candidate. When every matching mandate in a scan is marker-finished
and no request is unfinished for that repository, the checkpoint advances
to the creation time of the newest scanned comment. An exact mandate whose
marker lookup fails transiently records `transient_start` in request state
before the tick returns, so later ticks resume it under the same 24-hour
limit. The source lock key lower-cases the repository name, so differently
cased configuration for one GitHub repository still shares the same lock.

A finished request removes its worktree and keeps its control directory.

## Known limits

The Git runner API used by this worker does not expose a child-environment
override. The rebase commands pass `-c core.editor=true` and
`-c sequence.editor=true`, but ambient `GIT_EDITOR`, `GIT_SEQUENCE_EDITOR`,
`VISUAL`, and `EDITOR` take precedence over Git configuration. The account Git
runner is therefore responsible for removing those four variables from the
child environment.

## Not in this revision

- Spine task and review-gate integration for the rebased head
- An inner review lane
- Daemon auto-start of conflict workers
- Hub-issued mandates
- Non-content conflicts
- Branches containing merge commits
- Repositories without a check command
