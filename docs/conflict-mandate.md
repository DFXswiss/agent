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

### Operator requirements

Checkout and rebase honour the pull request's `.gitattributes`, so Git would run
`filter.*` and `merge.*.driver` programs; the query below also refuses
`diff.*` drivers because the worker runs Git commands that honour them. The
preflight therefore reads the system and global Git configuration with the
account Git runner (`git config --system --get-regexp` and `git config --global
--get-regexp` with the pattern
`^(filter\..*\.(clean|smudge|process)|merge\..*\.driver|diff\..*\.(textconv|command))$`).
Any hit is `preflight blocked: the worker Git configuration defines filter,
merge or diff drivers (<names>); the conflicts worker refuses to run
with them`. Directly after the clone the same query runs with `--local`; a hit
there is the blocker `repository configuration defines Git drivers`, and the
worktree and separate Git directory are removed directly. The operator
must therefore keep those drivers out of the worker account's system, global,
and repository configuration.

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
   local protected-name list covers `develop`, `main`, and `master`; after the
   pull-request read and the branch-name checks, and before any clone, the
   script also reads `repos/<head-repo>/branches/<url-encoded head ref>` with
   the worker account. `protected: true` finishes with the same blocker,
   `refusing to push protected head ref <head-ref>`, so a non-standard
   protected branch such as `release/2026.11` is refused too. An unreadable
   response is transient. Repository permissions are readable only when
   `permissions.push` is a boolean. Pull-request reads require `state` to be
   the string `open` or `closed`; string `head.sha`, `head.ref`, `base.ref`,
   and `base.sha`; a string `head.repo.full_name`, except that `head.repo: null`
   is the blocker `head repository is gone`; and `mergeable` true, false, or
   null. Comment listings require a list of objects with integer `id`, string
   `body`, string `user.login`, string `user.type`, GitHub-parsable string
   `created_at` and `updated_at`, and at least one string `html_url` or
   `issue_url` that contains a parsable pull or issue number. A timestamp that
   does not parse makes the listing transient for that tick.
   Reaction listings require objects with string `user.login` and string
   `content`.
   Branch protection requires a boolean `protected`, and the publication ref
   read requires a 40-hex `object.sha`. Missing, malformed, or wrongly typed
   data in any of these reads is transient; discovery never advances the
   checkpoint past a comment whose response is unreadable. The
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
   between the checks and the push. A change removes the worktree and the
   separate Git directory directly and finishes with the blocker
   `lane changed the repository metadata` without running Git in that worktree
   again, not even `git rebase --abort`. The same no-Git rule applies after a
   failed Git-directory comparison: the worktree and Git directory are removed
   directly and the abort is skipped. Local Git commands of this worker pass
   `-c core.hooksPath=/dev/null` and `-c core.fsmonitor=false`.
   Network `clone`, `fetch`, and `push` commands pass no global `-c` override.
   Directly after the clone writes `core.hooksPath=/dev/null`
   into the repository configuration with a local command, so later transfers
   use the same setting from the separate Git directory outside the worktree.
6. It refuses merge commits. It rebases the feature branch onto
   `origin/<base>` with the account's signed-commit configuration. If the
   starting rebase or a later `git rebase --continue` fails and no unmerged
   path exists, it aborts the rebase and finishes with the blocker
   `rebase failed` plus the masked, bounded tail of Git's output. That is a
   blocker, not a transient failure.
7. For only `UU` and `AA` regular text conflicts, it writes a lane spec outside
   the worktree, starts the configured implementer lane, and verifies the
   change set, conflict markers, and `git diff --check`. A conflict marker is a
   line that starts with exactly seven `<`, `=`, `>`, or `|` characters
   followed by whitespace or the end of the line, so bare and labelled forms
   are detected and eight or more of the character are not a marker. The allowed change set
   is measured against the index: the union of `git diff --name-only -z`,
   `git ls-files --others -z` including ignored files, and the unmerged paths
   must stay within the conflicted paths. Files that Git staged cleanly while
   replaying the commit are equal in the worktree and the index and are not
   reported. The same measurement runs immediately before the lane; a path
   outside the conflicted set there finishes with the blocker
   `unexpected worktree state before the lane`. Beyond that measurement, the
   script hashes the index directly before the lane starts and directly after
   it with `git ls-files --stage -z` plus `git ls-files --unmerged -z`. A
   difference finishes without a retry: it aborts the rebase and posts one
   blocker, `lane changed the index`, with no push (except when the same run
   already established that Git metadata changed, in which case no Git command
   runs and cleanup is direct). The index comparison runs
   directly after the lane attempt and before its `RESULT:` line,
   conflict-marker verification, or change-set measurement, so an index change
   cannot be replaced by another lane verdict. If starting or running the lane
   raises or returns a non-`complete` status, the script still takes the
   post-run index fingerprint and the `.git` file check before deciding. A
   changed index or `.git` file is the corresponding final blocker. With an
   unchanged index and `.git` file, a lane status of `partial` or `timeout`
   receives exactly one retry of the same stop with that failure stated in the
   spec. A second such failure aborts the rebase and posts the blocker `lane
   did not complete` with the status. A lane status of `unavailable` is
   transient. An exception while starting the lane process itself means the
   lane never ran and is also transient when the index and `.git` file are
   unchanged. Every attempt takes
   its own fingerprints directly before and
   after that attempt's lane run. Lane stdout and stderr pass through the
   worker-aware masking before they reach the lane log in the control
   directory, the lane-run record,
   or any returned line. The base branch and commit subject are redacted before
   they enter the spec. Unsupported head or base
   branch names finish the request with a blocker before any clone. A failed
   first result for another verification failure is retried once with the exact
   failure in the spec. Other unmerged states, a second failure, or too many
   stops abort the rebase and post one blocker. A lane answer of `RESULT:
   blocked` is not itself a transition: the deterministic verification (index
   fingerprint, `.git` file, change-set measurement, and conflict markers) runs
   after every lane run. If verification passes, the request continues even
   when the lane said `blocked`. If it fails and the lane said `blocked`, there
   is no retry: the script aborts the rebase and posts the deterministic failure
   as the blocker reason, plus the lane's first sentence as the separate bounded
   `Lane note` fact. If it fails and the lane did not say `blocked`, the single
   retry applies. The commit subject shown to the lane is read from
   `REBASE_HEAD`; when that read fails, the spec says `Commit being replayed:
   unavailable` and never substitutes the subject of `HEAD`.
8. It verifies the clean signed head, base ancestry, and every commit in
   `origin/<base>..HEAD`, then runs `check_argv` with a bounded timeout. The
   checks run repository code with file-system access to the request's control
   directory, so directly before they start the script records the bytes of
   `<git-dir>/config` and the sorted list of entries in `<git-dir>/hooks` with
   their sizes. Directly after the checks, before any Git command runs in the
   worktree again, it compares both by reading those files directly, without a
   Git invocation. A difference finishes with the blocker `checks changed the
   repository state`; the worktree and Git directory are removed without
   running Git in them, and nothing is pushed. Those two direct verifications
   run after every check return, including a non-zero exit, a timeout, and a
   raised error; a metadata difference keeps this blocker ahead of the check
   result. The same direct cleanup and
   no-Git rule applies when the pre-push `.git` file verification fails. The
   hook comparison records the
   entry names and their sizes and does not hash a hook's content. After the
   checks it recomputes
   the reviewed range, requires the same commit list and the same
   `origin/<base>` sha as before the checks, and verifies every commit in the
   recomputed range as signed. A failing check's redacted output tail is
   bounded before publication. The check child inherits the device environment
   without the ambient GitHub token variables and with an empty unconfigured
   GitHub profile, plus the
   `AGENT_CONFLICTS_HEAD`, `AGENT_CONFLICTS_BASE`, `AGENT_CONFLICTS_REPO`,
   `AGENT_CONFLICTS_PR`, `AGENT_CONFLICTS_SESSION`, and
   `AGENT_CONFLICTS_WORKTREE` variables. `AGENT_CONFLICTS_BASE` is the sha of
   `origin/<base>` fetched and verified in this worktree, not the base sha from
   the earlier pull-request read. The result facts store and report that same
   verified sha as `Base: <base-ref> <verified-sha>`.
9. Immediately before the push, it re-reads the pull request and the head-branch
   protection with the worker account. The pull request must still be open, its
   head sha must still equal the head sha observed at the start of the request,
   its head repository, head ref, and base ref must be unchanged, its
   `base.sha` must equal the sha of `origin/<base>` fetched and verified in this
   worktree, and the branch must not be protected. A moved head or base, or an
   unreadable response, is transient:
   nothing is pushed and the next tick starts again from the pull-request read.
   A closed pull request, changed refs or repository, or newly protected branch
   is a blocker without a push. It then pushes only with
   `--force-with-lease=refs/heads/<head-ref>:<old-head>`. The account runner
   accepts exactly that lease form: the value is split at the last colon, the
   ref part must start with `refs/heads/` and contain no whitespace or colon,
   and the expected value must be 40 hexadecimal characters. A branch name may
   contain `=`. Any other `refs/...` namespace is rejected. Before the push it
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
   posting a body rendered before the push. On the resume path of a request in
   phase `pushing`, an unreadable pull request is a transient failure like any
   other of a known request: `transient_start` is recorded once, the tick
   returns a transient line, and after 24 hours the request finishes with a
   blocker instead of posting the stored result.

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
`<details>` block. The fixed EN and DE sentences stay plain text. Every dynamic
value passes through `safe` and `redact`. Unicode control characters and
bidirectional or invisible formatting characters are removed, a tab becomes
one space, and the value is placed in Markdown code formatting: single-line
values in a code span with any backtick replaced by `'`, and multi-line values
in a fenced code block whose fence is longer than any backtick run in the
value.
GitHub therefore renders no mention, link, HTML, or heading from a reason,
check-output tail, lane note, file name, branch name, sha, or count. Local
paths never appear.

## Local state and failure classes

The session control directory holds lane specs, lane logs, the separate Git
directory, and `state.json`; the session checkpoint holds the newest processed
comment timestamp per repository. State writes are atomic. Local state and the
checkpoint suppress reprocessing of terminal requests. GitHub markers
additionally finish a request whose local state is missing. Deleting a marker
comment does not restart a request. Lane runs are recorded through `lane.run`;
there is no new table or activity type.

Every blocker pushes nothing, posts exactly one blocker, and finishes that
request. A blocker aborts an in-progress rebase unless Git metadata was found
changed; in that case the worktree and separate Git directory are removed
directly and no Git command runs. A new comment is needed to try again.
Unreadable GitHub or network state, unknown `mergeable`, and a rejected push
   lease post nothing and retry later from the pull-request read. After a failed
   push, the worker reads the publication branch through the worker account's
   GitHub API as `repos/<head-repo>/git/ref/heads/<url-encoded head-ref>` and
   uses `object.sha`. A push that ended because the bounded runner timed out is
transient; it does not become a blocker even when the remote head is unchanged.
If that read fails, the failure is transient. If the remote head differs from
the observed old head for this request, the lease was rejected, so it is
transient and the next tick starts from the pull-request read. A push that
returned on its own with a non-zero exit code, followed by a successful
remote-head read that still shows the observed old head, becomes the blocker
`push was refused by the remote` only when the push output carries a remote
refusal: a `remote:` line, `! [remote rejected]`, `! [rejected]`,
`pre-receive hook declined`, `protected branch`, `GH0`, `Permission to ...
denied`, or `403`; it includes the masked, bounded tail of the push output. An
unchanged head without such a sign, including a transport failure or empty
output, is transient. After 24 hours of transient failures in any other phase,
including `result-pending` and a failing marker lookup before resumption,
they finish with a blocker. A blocker comment that cannot be posted remains in
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
An unreadable pull-request response for a mandate is recorded as transient
request state and never lets the checkpoint advance past that comment, even
when a later readable mandate is processed in the same tick. When a comment's
URL identifies a pull request, a 404 for that pull request is such an
unreadable response; only a comment whose URL identifies a plain issue is
skipped. A malformed
comment listing is unreadable for the whole tick and likewise advances no
checkpoint.

A finished request removes its worktree and keeps its control directory.
When the clone, or any step before the first successful checkout, fails, the
worktree and the separate Git directory are removed directly. A blocker runs
`git rebase --abort` only when the worktree's `.git` file exists and metadata
verification has not failed; otherwise it removes both directories directly.

## Trust boundary of checks

`check_argv` is trusted operator configuration. It lives in
`$AGENT_HOME/conflicts.json`, which only the operator writes, and the script
runs that command as written. It is not, however, a command of this feature:
it executes code of the pull request under review with the worker's
environment minus the ambient GitHub token variables and with an empty
unconfigured GitHub profile. Isolating that execution (container, sandbox,
separate user) is the operator's responsibility, and this feature does not
provide it. The repository-state comparison of step 8 detects Git metadata
that the checks changed, and the pre-push verification in step 9 rejects a
changed worktree or a moved head with the same blocker; neither is a sandbox
boundary for anything else the checks do on the device. Configure the worker
only for repositories and `request_logins` whose pull requests may run code on
that device, and only where the operator accepts that execution.

## Known limits

Local state is an optimization. If the control directory of a request is lost
after a successful push and before the result comment was posted, the next tick
finds a mergeable pull request without a marker and reports that there are no
conflicts instead of the facts of the push.

An absolute path that contains whitespace is masked up to the first whitespace
when it appears inside free text such as check output. The configured
`workspace_root`, the control directory, and the home directory are replaced
verbatim wherever they appear.

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
- conflicted files whose names Git prints C-quoted (tab, newline, double quote, backslash)
- Branches containing merge commits
- Repositories without a check command
- A conflicts worker GitHub account with `command_prefix` or `worktree_paths`
  in `github-accounts.json`; preflight refuses it in this revision
