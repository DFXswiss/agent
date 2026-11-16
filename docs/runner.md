# The runner

`agent runner` manages detached agent jobs on this device. Today it reads
GitHub notifications and queues the mentions it is admitted for (`ingest`),
supervises and cleans up jobs (`supervise`, `prune`, `retry`), and reports on
the queue and the configuration (`status`, `doctor`). Starting jobs
(`dispatch`) refuses until the worker lands; see "Not yet". The command group
owns the external effects; the rules it applies live in the tested modules
`jobs`, `ingest`, `dispatch`, `finalize`, `reap`, `queue` and `health`.

The runner lives under `$AGENT_HOME/runner` and uses no other directory. Every
subcommand resolves its paths from that base, so moving `$AGENT_HOME` moves the
whole runner.

## Layout

| Path | Purpose |
| --- | --- |
| `policy.json` | Admission policy: who may ask, which repositories and job types, and the GitHub login this runner must act as. |
| `runner.json` | Runtime settings: capacity, budgets, deny-lists, retention. |
| `skills-catalog.json` | The skills this installation publishes. |
| `agent.json` | The agent pinning checked by `doctor`. |
| `state/tmux.sock` | The private tmux socket every job session uses. |
| `state/repos/` | Bare clones, one per repository. |
| `state/work/` | One worktree per job, named by job id. |
| `state/logs/<id>.transcript` | Everything the job wrote. |
| `state/logs/<id>.exit` | The job's exit code, written atomically. |
| `state/runner.lock` | The one lock the mutating commands share. |

Configuration files are never created by these commands. A missing or
unreadable file stops the command; it is not replaced by defaults. The runtime
directories under `state/` are created on demand.

## Policy and runner configuration

`policy.json` answers *who may ask for what*. It is the only file `ingest`
reads, apart from the login check:

```json
{
  "expected_gh_user": "example-bot",
  "enabled": true,
  "actors_allow": ["example-operator"],
  "repos_allow": ["example-owner/example-repository"],
  "job_types_allow": ["pr-review"],
  "default_skill_on_mention": "pr-review"
}
```

`expected_gh_user` is required. Both `ingest` and `supervise` run
`gh api user --jq .login` first and stop unless it matches the policy
case-insensitively. A runner authenticated as another account would attribute
every action to that account, so the check is fail-closed rather than a warning.

`runner.json` answers *how this device runs work*. `supervise` and `prune` read
this file; they do not read the policy, with the one exception that `supervise`
reads `expected_gh_user`. `dispatch` will read `max_concurrent` here once the
worker exists; today it refuses before reading any file. `retry` needs no
setting and reads neither file.

```json
{
  "max_concurrent": 1,
  "clone_stall_minutes": 5,
  "defaults": {
    "timeout_minutes": 30,
    "stall_minutes": 5,
    "deny": ["Bash"]
  },
  "skills": {
    "pr-review": {"done_kind": "pr-reviewed", "deny": ["Bash", "Edit"]}
  },
  "retention_days": {"done": 7, "failed": 30}
}
```

`max_concurrent` must be an integer of at least 1. `retention_days` is read per
state; an absent state is reported as *not configured* and nothing in it is
pruned.

## Subcommands

### `agent runner ingest --session ID`

Reads `policy.json`, verifies the GitHub login, scans notifications through
`gh`, and inserts a queued row for every admitted mention. The session id is
recorded on the row and may contain only letters, digits, `_` and `-`.
De-duplication uses the whole store, including rows written by other devices,
so the same request is one job.

Output: `job.queued <job-id>` per new job, or `job.queued none`; skipped
notifications are counted on standard error.

### `agent runner dispatch`

Refuses until the worker exists, because a job started with nothing to run it
would never finish. Once the worker lands, it will start queued jobs while
capacity allows. For each job it ensures the bare clone exists, fetches,
creates a detached worktree, records an output baseline, and starts a detached
tmux session that runs the worker. The worktree holds the pull-request head,
checked out detached, for a pull-request job, and the repository's default
branch for an issue job; `ref` stays the number. A probe that cannot answer
whether the number is a pull request leaves the job queued rather than choosing
a commit. Stale `.exit` and `.transcript` files of queued jobs are removed
first: a queued job has no live worker, and a leftover exit code would
otherwise be read as the new attempt's result.

Output: `job.started <job-id>` per job, or `job.started none`.

### `agent runner supervise`

Once a worker runs jobs, finalizes those that have exited or whose tmux session
is gone, then reaps worktrees and sessions left behind by killed workers. Until
the worker lands, no row on this device reaches `running`, so there is nothing
to finalize. A row whose exit code is missing but whose session is alive is
measured against the timeout and stall budgets; a row whose session has
disappeared is recorded as crashed.

Output: `job.finalized <job-id>` (or `job.finalized none`),
`worktree.removed <job-id>` and `session.killed <name>` per item, with failures
reported as `reap failed <item>` on standard error. Any failed reap step makes
the command exit with status 1 so a schedule notices.

This command is distinct from `agent supervise --session`, which watches one
ledger session.

### `agent runner prune`

Deletes finished queue rows and their log files once they are older than the
retention configured for their state. Each of `done` and `failed` is handled
independently; when a state has no retention setting, the command prints
`prune <state> not configured` and keeps everything in that state. A leftover
worktree under `state/work/<id>` is removed first with the same guards as
`retry`; a row whose worktree cannot be removed safely is kept with its log
files and reported as `prune skipped <job-id>: <reason>` on standard error.
Output: `job.pruned <job-id>` per row, or `job.pruned none`.

### `agent runner retry JOB_ID`

Re-queues a job owned by this device, increments its attempt count, clears the
previous attempt's result fields, and removes its log files and leftover
worktree. The worktree is removed through `git worktree remove` and
`worktree prune`. Only if the directory still exists is a recursive delete
used, and only when the resolved path is strictly inside `state/work` and is
not a symlink; anything else is refused rather than deleted.

Output: `job.retried <job-id>`.

### `agent runner status`

Prints queue counts and the running jobs. Read-only, so it takes no lock.

```text
jobs queued=2 running=1 done=14 failed=0
job.running <job-id> 2026-11-13T10:00:00Z
```

### `agent runner doctor`

Reads all four configuration files and the environment, and reports every
problem it can find rather than stopping at the first. It runs the three
health checks and verifies `max_concurrent`. It requires the binaries `gh`,
`git`, `tmux` and `ps` on `PATH`. It checks that the runner directories are
writable, and that the authenticated GitHub login matches the policy.

Output: one `doctor problem: <text>` line per problem followed by
`doctor <n> problems`, exiting 1; or `doctor ok`.

## The runner lock

`ingest`, `dispatch`, `supervise`, `prune` and `retry` take one exclusive,
non-blocking `flock` on `state/runner.lock`. When another runner command holds
it, they print `runner busy` and return success; the schedule tries again on its
next pass. `dispatch` is listed because the sequence guarded below runs once the
worker exists, but today it refuses before it takes the lock.

The lock exists because dispatch is a sequence of external steps. Between
creating a worktree and writing the row as running, the worktree belongs to a
queued job and looks like an orphan. A concurrent `supervise` would remove it
mid-construction. Serialising the mutating commands makes that impossible,
which is also why no separate "preparing" marker is written: there is no
concurrent reaper to protect the worktree from. Until a worker exists, nothing
reaches that sequence, so the lock serialises only `ingest`, `supervise`,
`prune` and `retry`.

`status` and `doctor` are read-only and take no lock.

## Only this device's jobs

Every command acts on job rows this device owns. Rows replicated from another
device are ignored by `dispatch`, `supervise`, `prune`, `retry` and `status`,
and a retry of such a row is refused. Rows whose id is not a plain name
(`[A-Za-z0-9_-]`) are ignored by every command as well, because a job id
becomes a file name under `state/logs` and `state/work`. The commands that
run jobs also ignore a row whose stored `worktree` is anything but
`state/work/<id>`, since supervision hands that path to
`git worktree remove --force`. `ingest` is the
deliberate exception: it reads the whole store for de-duplication, because a
mention already queued by another device must not be inserted again.

## Not yet

- The worker that runs a job: a static script that launches lanes through the
  AI configuration (`ai-accounts.json`) as DESIGN.md §19.1 and §19.8 require,
  together with the prompt templates it renders. Until it lands,
  `agent runner dispatch` refuses to start jobs.
- GitHub announce and outcome comments for a job.
- A schedule or service unit for the subcommands.
- Container packaging.
- Owner wildcards (`owner/*`) in `repos_allow` and job-type detection from the
  catalogue. Until this lands, a policy that lists only `owner/*` admits
  nothing: admission is fail-closed.
- The mergeable probe for `pr-mergeable` jobs; those jobs record
  `work_performed` as `unknown` until it is ported.
