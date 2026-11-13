# The runner

`agent runner` runs detached agent jobs on this device. It reads GitHub
notifications, queues the mentions it is admitted for, starts a worker in a
private worktree for each queued job, supervises the workers, and prunes
finished queue rows. The command group owns the external effects; the rules it
applies live in the tested modules `jobs`, `ingest`, `dispatch`, `finalize`,
`reap`, `queue` and `health`.

The runner lives under `$AGENT_HOME/runner` and uses no other directory. Every
subcommand resolves its paths from that base, so moving `$AGENT_HOME` moves the
whole runner.

## Layout

| Path | Purpose |
| --- | --- |
| `policy.json` | Admission policy: who may ask, which repositories and job types, and the GitHub login this runner must act as. |
| `runner.json` | Runtime settings: capacity, budgets, agent command, deny-lists, retention. |
| `skills-catalog.json` | The skills this installation publishes. |
| `agent.json` | The agent pinning checked by `doctor`. |
| `prompts/<job_type>.md` | Prompt template for one job type. |
| `state/tmux.sock` | The private tmux socket every worker session uses. |
| `state/repos/` | Bare clones, one per repository. |
| `state/work/` | One worktree per job, named by job id. |
| `state/logs/<id>.transcript` | Everything the worker wrote. |
| `state/logs/<id>.exit` | The worker's exit code, written atomically. |
| `state/logs/<id>.prompt` | The rendered prompt for the current attempt. |
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

`runner.json` answers *how this device runs work*. `dispatch`, `supervise`,
`prune` and the worker read this file; they do not read the policy, with the
one exception that `supervise` reads `expected_gh_user`. `retry` needs no
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
  "retention_days": {"done": 7, "failed": 30},
  "agent": {
    "command": "agent-cli",
    "args": ["run"],
    "prompt_flag": "--prompt"
  }
}
```

`max_concurrent` must be an integer of at least 1. `retention_days` is read per
state; an absent state is reported as *not configured* and nothing in it is
pruned. A worker's deny-list is the job type's own list when it has one and
`defaults.deny` otherwise. A skill entry without a deny-list falls back to the
defaults rather than running unrestricted.

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

Starts queued jobs while capacity allows. For each job it ensures the bare
clone exists, fetches, creates a detached worktree, records an output baseline,
and starts a detached tmux session that runs the worker. Stale `.exit` and
`.transcript` files of queued jobs are removed first: a queued job has no live
worker, and a leftover exit code would otherwise be read as the new attempt's
result.

Output: `job.started <job-id>` per job, or `job.started none`.

### `agent runner supervise`

Finalizes workers that have exited or whose tmux session is gone, then reaps
worktrees and sessions left behind by killed workers. A row whose exit code is
missing but whose session is alive is measured against the timeout and stall
budgets; a row whose session has disappeared is recorded as crashed.

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
`prune <state> not configured` and keeps everything in that state. Output:
`job.pruned <job-id>` per row, or `job.pruned none`.

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
health checks, verifies `max_concurrent`, and validates the `agent` block
(`command`, `args` and `prompt_flag`). It requires the binaries `gh`, `git`,
`tmux` and `ps` on `PATH`, and the agent command too when the block is usable.
For every job type in `job_types_allow` it checks that `prompts/<type>.md`
exists — that is the template the worker renders, and the catalogue's skill ids
are a different namespace. It checks that the runner directories are writable,
and that the authenticated GitHub login matches the policy.

Output: one `doctor problem: <text>` line per problem followed by
`doctor <n> problems`, exiting 1; or `doctor ok`.

### `agent runner work`

The worker entry point. It runs inside a job's tmux session, with the worktree
as its working directory, and is not meant to be started by hand. The job id is
the worktree directory name. A directory that is not a direct child of
`state/work` is rejected with status 2 before anything is written.

After identifying the job it reads `runner.json`, renders
`prompts/<job_type>.md` into `state/logs/<id>.prompt`, and runs the configured
agent with the resolved deny-list. The agent's output is copied to standard
output and to `state/logs/<id>.transcript` as it arrives. Every failure after
job identification appends a line to the transcript and writes exit status 2
(127 when the command cannot start), so supervision never waits forever on a
worker that died early. The worker returns the agent's own exit status.

The worker session inherits the environment of the runner's tmux server, so
settings such as `AGENT_PG_DSN` must be present in the environment that runs
`dispatch`. The wrapper passes `AGENT_HOME` explicitly and puts nothing else
on the command line, because a command line is visible to every local user.

## The runner lock

`ingest`, `dispatch`, `supervise`, `prune` and `retry` take one exclusive,
non-blocking `flock` on `state/runner.lock`. When another runner command holds
it, they print `runner busy` and return success; the schedule tries again on its
next pass.

The lock exists because dispatch is a sequence of external steps. Between
creating a worktree and writing the row as running, the worktree belongs to a
queued job and looks like an orphan. A concurrent `supervise` would remove it
mid-construction. Serialising the mutating commands makes that impossible,
which is also why no separate "preparing" marker is written: there is no
concurrent reaper to protect the worktree from.

`status` and `doctor` are read-only and take no lock. `work` takes no lock: it
runs inside the worker session and must remain runnable while a supervising
command holds the lock.

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

- GitHub announce and outcome comments for a job.
- Prompt templates shipped with the package.
- A schedule or service unit for the subcommands.
- Container packaging.
- Owner wildcards (`owner/*`) in `repos_allow` and job-type detection from the
  catalogue. Until this lands, a policy that lists only `owner/*` admits
  nothing: admission is fail-closed.
- The mergeable probe for `pr-mergeable` jobs; those jobs record
  `work_performed` as `unknown` until it is ported.
