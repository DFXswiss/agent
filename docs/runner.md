# The runner

`agent runner` manages detached agent jobs on this device. It reads GitHub
notifications and queues the mentions it is admitted for (`ingest`), starts
queued jobs (`dispatch`), runs one job inside its worktree (`work`), supervises
and cleans up jobs (`supervise`, `prune`, `retry`), and reports on the queue and
the configuration (`status`, `doctor`). The command group owns the external
effects; the rules it applies live in the tested modules `jobs`, `ingest`,
`dispatch`, `finalize`, `reap`, `queue`, `health` and `runner_worker`.

The runner's own files live under `$AGENT_HOME/runner`, and every subcommand
resolves those paths from that base, so moving `$AGENT_HOME` moves its
configuration and state. Job rows are not files there: the commands read and
write them in the device store, which lives with the ledger. The worker also
reads the device-level `$AGENT_HOME/ai-accounts.json` (lane model and account
selection) and `$AGENT_HOME/github-accounts.json` (the GitHub account bound to a
session). The provider profile a lane starts with is the account's `config_dir`
from `ai-accounts.json`; it may point outside `$AGENT_HOME`.

## Layout

| Path | Purpose |
| --- | --- |
| `policy.json` | Admission policy: who may ask, which repositories and job types, and the GitHub login this runner must act as. |
| `runner.json` | Runtime settings: capacity, budgets, deny-lists, retention. |
| `skills-catalog.json` | The skills this installation publishes. |
| `agent.json` | The agent pinning checked by `doctor`. |
| `prompts/pr-review.md` | The operator's template for the reviewer prompt the worker renders. |
| `state/tmux.sock` | The private tmux socket every job session uses. |
| `state/repos/` | Bare clones, one per repository. |
| `state/work/` | One worktree per job, named by job id. |
| `state/logs/<id>.transcript` | Everything the job wrote. |
| `state/logs/<id>.prompt` | The rendered reviewer prompt for the job's one lane. |
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

`runner.json` answers *how this device runs work*. `dispatch`, `supervise` and
`prune` read this file. `retry` needs no setting and reads neither this file nor
the policy. `dispatch` also reads `policy.json` to refuse a policy that allows a
job type the worker does not implement.

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

The `deny` lists are not consulted by the worker. It starts one `reviewer` lane
and that role's access is `read-only` in `ai-accounts.json`, so the launcher's
fixed read-only tool set, not a runner deny-list, decides what the lane can use.
`doctor` still validates the configured deny-list entries against the skills
catalogue; that check exists for the other workflows those settings serve.

## Subcommands

### `agent runner ingest --session ID`

Reads `policy.json`, verifies the GitHub login, scans notifications through
`gh`, and inserts a queued row for every admitted mention. The session id is
recorded on the row and may contain only letters, digits, `_` and `-`.
De-duplication uses the whole store, including rows written by other devices,
so the same request is one job. A notification that led to a job is marked
read, so a pruned job does not come back on the next pass by itself; a later
mention makes the thread unread again and is evaluated afresh.

Output: `job.queued <job-id>` per new job, or `job.queued none`; skipped
notifications are counted on standard error. If the notification listing itself
cannot be read as a list of pages, the command prints
`ingest failed: notifications listing unusable` on standard error, exits 1, and
prints no `job.queued` line: an unread listing is not an empty queue.

### `agent runner dispatch`

Starts queued jobs while capacity allows. Before taking the lock it checks that
the worker can run at all: `prompts/pr-review.md` exists, `ai-accounts.json`
names models and declares `lane_execution`, and `policy.json` names no job type
without a worker. A failure stops the command before any row or worktree is
created. For each job it ensures the bare clone exists, fetches, creates a
detached worktree, records an output baseline, and starts a detached tmux
session that runs `agent runner work` with `AGENT_HOME` set. The worktree holds
the pull-request head, checked out detached, for a pull-request job, and the
repository's default branch for an issue job; `ref` stays the number. A probe
that cannot answer whether the number is a pull request leaves the job queued
rather than choosing a commit. Stale `.exit` and `.transcript` files of queued
jobs are removed first: a queued job has no live worker, and a leftover exit
code would otherwise be read as the new attempt's result.

Output: `job.started <job-id>` per job, or `job.started none`.

### `agent runner work`

The detached worker for one job. `dispatch` starts it with the job worktree as
its working directory; an operator does not normally run it. It performs one
`pr-review` job and writes `state/logs/<id>.exit` last:

1. Identify the job from the current directory: it must be exactly the direct
   child worktree named by a safe job id. Require the row to be this device's,
   to be `running`, and to carry a non-empty repository, numeric
   `ref`, `pr-review` job type (case-insensitive), a session id matching
   `[A-Za-z0-9_-]+`, an integer `attempts`, and a stored `worktree` equal to
   `state/work/<id>`.
2. Read `prompts/pr-review.md`. It is the operator's template; the worker
   substitutes the literal placeholders `{{REPO}}`, `{{NUMBER}}`, `{{BASE}}`,
   `{{HEAD}}` and `{{JOB_ID}}` and adds nothing else before the fixed data
   section.
3. Read `policy.json` and require `expected_gh_user`. Read
   `github-accounts.json`, bind the session's account, and require its login to
   equal `expected_gh_user` case-insensitively. Finalize counts only the runner
   login's outputs, so posting as another account could never complete the job.
4. Resolve the checked-out `HEAD^{commit}` locally and require a 40-character
   lowercase SHA. Read the pull request's base branch through that account with
   `gh api .../pulls/<n> --jq .base.ref` and accept it only when
   `git check-ref-format --branch` accepts it. Take the local three-dot diff
   `refs/remotes/origin/<base>...HEAD`. An empty diff fails, and a diff over
   400,000 characters fails rather than being truncated: the lane always
   reviews exactly the change that was checked out.
5. Write the rendered prompt atomically to `state/logs/<id>.prompt`. Its fixed
   section marks everything below as repository data, fences the diff with a
   fence longer than any backtick run in it, and states the output contract:
   the review is the final message, ends with exactly `STATUS: complete` and
   either `RESULT: approved` or `RESULT: rejected`, and the lane must not run
   commands, contact GitHub or start other agents.
6. Start exactly one `reviewer` lane through `lane.launch` with model slot
   `first` (`ai-accounts.json`), `tmux=False`, the worktree as its directory and
   the agent home as its configuration home. `reviewer` is a read-only workflow
   kind, so a writable binding is refused. While the lane runs, a heartbeat
   appends `worker: review lane running` to the transcript so the stall
   watchdog sees progress.
7. Require `STATUS: complete` and `RESULT: approved` or `RESULT: rejected`,
   remove the protocol lines, and refuse an empty review or a formatted body
   over 60,000 characters.
8. Insert a deterministic `review.post` (COMMENT) bound to the checked-out
   head and to the GitHub account validated before the lane. Its id includes
   the job, the attempt count and the head, so a retry gets its own row while
   re-running one attempt reuses its own. A row already `done` is not executed
   again; an `error` row is reset to `pending`. Because the row carries that
   account binding, a session binding that changes during the lane cannot post
   the review through another login: the executor refuses the switch.
9. Execute the activity through the script's GitHub executor, require the row
   to be `done`, append `worker: review posted`, and write the exit file.

Every progress line is also written to the job's standard output with the
`worker: ` prefix. Any missing or unusable input, a lane that does not report
`STATUS: complete` and an approved/rejected result, an empty or oversized
review, or a failed post writes a non-zero exit code and a fixed failure line to
the transcript. The worker takes no runner lock: it runs for the whole job, and
dispatch starts it from the worktree immediately before writing the row as
running, so the worker waits up to 120 seconds for that handoff, re-reading the
row until it is this device's `running` row with a `worktree`, before it
validates the job.

### `agent runner supervise`

Finalizes jobs whose worker has exited or whose tmux session is gone, then reaps
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

Reads the runner configuration files, `ai-accounts.json`, and the environment,
and reports every problem it can find rather than stopping at the first. It
runs the three health checks and verifies `max_concurrent`. It requires the
worker's model selection and `lane_execution` declaration, requires
`prompts/pr-review.md` when the policy allows `pr-review`, and reports every
allowed job type that has no worker. It requires the binaries `gh`, `git`,
`tmux` and `ps` on `PATH`. It checks that the runner directory and each existing
directory under `state/` (`state`, `repos`, `work`, `logs`) is a writable
directory; a missing one is created on first use. It also checks that the
authenticated GitHub login matches the policy; a failed `gh` call is reported as
`gh login check failed` without copying its stderr.

Output: one `doctor problem: <text>` line per problem followed by
`doctor <n> problems`, exiting 1; or `doctor ok`.

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

`work` deliberately takes no lock. It runs for the whole job, not for one short
command; taking the lock would make every other runner command print
`runner busy` for the job's duration, and waiting would serialize the queue
behind a single lane. Dispatch starts the worker just before writing the row as
running; the worker waits for its own queued row to become running, changes no
worktree, and races with no mutating command.

`status` and `doctor` are read-only and take no lock. The worker is not a
mutating runner command, so it neither takes nor waits for this lock.

## Only this device's jobs

Every command acts on job rows this device owns. Rows replicated from another
device are ignored by `dispatch`, `work`, `supervise`, `prune`, `retry` and
`status`, and a retry of such a row is refused. Rows whose id is not a plain name
(`[A-Za-z0-9_-]`) are ignored by every command as well, because a job id
becomes a file name under `state/logs` and `state/work`. The commands that
run jobs also ignore a row whose stored `worktree` is anything but
`state/work/<id>`, since supervision hands that path to
`git worktree remove --force`. `ingest` is the
deliberate exception: it reads the whole store for de-duplication, because a
mention already queued by another device must not be inserted again.

## Not yet

- Job types other than `pr-review` (`merge-conflict`, `pr-mergeable`, …) have
  no worker. Dispatch refuses a policy that allows one, and the worker fails a
  queued one instead of guessing.
- More prompt templates than `prompts/pr-review.md`.
- GitHub announce and outcome comments for a job.
- A schedule or service unit for the subcommands.
- Container packaging.
- Owner wildcards (`owner/*`) in `repos_allow` and job-type detection from the
  catalogue. Until this lands, a policy that lists only `owner/*` admits
  nothing: admission is fail-closed.
- The mergeable probe for `pr-mergeable` jobs; those jobs record
  `work_performed` as `unknown` until it is ported.
- Ingest parity: re-queueing a finished job when it is mentioned again while
  its row still exists. Today such a mention is skipped as a duplicate.
- @-mentions in a lane's review text are posted as written: the worker does
  not strip or resolve them.
