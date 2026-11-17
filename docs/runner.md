# The runner

`agent runner` manages detached agent jobs on this device. It reads GitHub
notifications and queues the mentions it is admitted for (`ingest`), starts
queued jobs (`dispatch`), runs one job inside its worktree (`work`), supervises
and cleans up jobs (`supervise`, `prune`, `retry`), and reports on the queue and
the configuration (`status`, `doctor`). The command group owns the external
effects; the rules it applies live in the tested modules `jobs`, `ingest`,
`dispatch`, `finalize`, `reap`, `queue`, `health`, `runner_worker` and
`runner_status`.

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

`policy.json` answers *who may ask for what*. With `skills-catalog.json` it is
what `ingest` reads, apart from the login check:

```json
{
  "expected_gh_user": "example-bot",
  "enabled": true,
  "actors_allow": ["example-operator"],
  "repos_allow": ["example-owner/*", "other/exact-repository"],
  "job_types_allow": ["pr-ready"],
  "default_skill_on_mention": "pr-review"
}
```

A `repos_allow` entry is either an exact `owner/repository` or `<owner>/*`,
which names every repository of that owner. `repos_deny` takes the same forms;
a wildcard there denies the whole owner. A pattern that is not an exact name or
`<owner>/*` (`*`, `*/*`, `owner/prefix*`, `owner/`) names nothing, so a typo
cannot widen the list. `private_repos_allow` stays exact names only.

`job_types_allow` names *queue types*, not skill ids. The catalogue maps each
skill to one:

```json
{
  "skills": [
    {
      "id": "pr-review",
      "queue_type": "pr-ready",
      "command_prefixes": ["/review"],
      "body_patterns": ["please review"]
    }
  ]
}
```

The skill id is what names a job and selects its worker and its `runner.json`
settings. The queue type is what admission compares with `job_types_allow`. A
mention is resolved to a skill by a command prefix on its first word or a body
phrase; a match whose queue type is not allowed is denied rather than replaced
by the default, so a request for a skill this instance does not run stays
refused. No match at all uses `default_skill_on_mention`, which must name a
catalogue skill id.

`expected_gh_user` is required. Both `ingest` and `supervise` run
`gh api user --jq .login` first and stop unless it matches the policy
case-insensitively. A runner authenticated as another account would attribute
every action to that account, so the check is fail-closed rather than a warning.

`runner.json` answers *how this device runs work*. `dispatch`, `supervise` and
`prune` read this file. `retry` needs no setting and reads neither this file nor
the policy. `dispatch` also reads `policy.json` and `skills-catalog.json` to
refuse a queue type that maps to no skill or to a skill whose worker does not
exist.

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

Reads `policy.json` and `skills-catalog.json`, verifies the GitHub login, scans
notifications through `gh`, and inserts or re-queues a row for every admitted
mention. A comment written by the runner's own login is never a request. The
session id is recorded on the row and may contain only letters, digits, `_` and
`-`.

A mention is identified by the comment that carries it
(`comment:<repo>:<ref>:<comment id>`), not by the thread. An existing row is
handled by its state: `queued` or `running` is left alone; `failed` is retried
with its attempt count incremented and the previous attempt's activity,
worktree and log files removed; `done` is queued again only when it is a pull
request whose current head SHA is known and differs from the SHA stored on the
row. A `done` row is otherwise a duplicate, and a row whose recorded trigger is
missing (a row written before triggers existed) is treated as already handled.
The recorded trigger is a high-water mark: only a comment newer than the
recorded one counts as a new request, so a deleted or edited later mention does
not bring back an earlier one. A new generation gets a fresh `created_at`, so it
also gets a new status comment. Rows written for another device are skipped
without marking the thread read. Where a thread is marked read without a new
job and the comment is newer than the recorded trigger — the job is queued or
running, or a finished job's head did not move — the handled comment is first
recorded as the row's `trigger`, so a later failure or head change that makes
the thread unread cannot select the same mention again. A path that leaves the
thread unread leaves the trigger unchanged.

The row carries `job_type` (the skill id), `queue_type`, `trigger` and
`head_sha` alongside the fields dispatch records later. `head_sha` is written
by ingest and overwritten by dispatch with the commit it checked out, so a
later mention compares the reviewed commit rather than the one ingest saw.
De-duplication uses the whole store, including rows written by other devices,
so the same request is one job. A notification that led to a job is marked
read, so a pruned job does
not come back on the next pass by itself; a later mention makes the thread
unread again and is evaluated afresh. A re-queue that cannot remove the
previous attempt's evidence leaves the thread unread so the next pass retries.

Output: `job.queued <job-id>` per new job, then `job.requeued <job-id>` per
re-queued job; `job.queued none` only when nothing was created and nothing was
re-queued. Skipped notifications are counted on standard error. If the
notification listing itself cannot be read as a list of pages, the command
prints `ingest failed: notifications listing unusable` on standard error, exits
1, and prints no `job.queued` line: an unread listing is not an empty queue.

### `agent runner dispatch`

Starts queued jobs while capacity allows. Before taking the lock it checks that
the worker can run at all: `ai-accounts.json` names models and declares
`lane_execution`, the catalogue is readable, and every queue type in
`policy.json` maps to a catalogue skill whose id has a worker (and, for
`pr-review`, that `prompts/pr-review.md` exists). An allowed queue type no
catalogue skill carries is refused here. A failure stops the command before any
row or worktree is created. For each job it ensures the bare clone exists, fetches, creates a
detached worktree, records an output baseline, and starts a detached tmux
session that runs `agent runner work` with `AGENT_HOME` set. The worktree holds
the pull-request head, checked out detached, for a pull-request job, and the
repository's default branch for an issue job; `ref` stays the number. A probe
that cannot answer whether the number is a pull request leaves the job queued
rather than choosing a commit. A pull-request job whose checked-out commit
cannot be resolved to a full lower-case commit id is likewise left queued,
with its worktree removed, rather than started under the head ingest stored,
which may name a different commit. Stale `.exit` and `.transcript` files of
queued jobs are removed first: a queued job has no live worker, and a
leftover exit code would otherwise be read as the new attempt's result.

Once the worktree exists and before the output baseline is read, dispatch posts
one comment on the issue or pull request: `Working on this now: job <id>.`, with
` (attempt N)` on a retry. The order is worktree → announcement → baseline. A
baseline taken before the announcement would miss the bot's own comment, and the
silent-failure check in `supervise` would count it as new output and pass every
job. An announcement that was not posted but is safely retired does not stop
the job. If it may still be posted later, the job stays queued and its worktree
is removed, exactly as on a baseline failure: that comment would otherwise
land after the baseline and count as the job's output. Either undelivered case
is reported as `announce failed <job-id>` on standard error. The comment is a
`comment.post` activity, never a direct `gh` call.

All status activities of one job generation share an `edit_marker`, the only
marker the executor accepts:
`<!-- agent-runner-job:<job id>:<UTC timestamp> -->`. The job id matches
`[A-Za-z0-9_-]+` without `--`, and the timestamp has the form
`YYYY-MM-DDTHH:MM:SSZ`. The announcement creates the comment the first time it
runs; every later status activity (a retry's announcement or a failure
outcome) edits that same comment in place, so the issue or pull request keeps
one status comment per job. The
announcement still has to precede the output baseline: the first announcement
is what creates the comment, and an edit never creates a new comment id, so a
later edit can never count as new output.

The row records `pull_request` (whether the probe answered that the number is a
pull request) and `announced` once the announcement ended `done`. Writing
`announced` at once means a later baseline failure, which leaves the job
queued, does not announce it again on the next pass. If that write fails, the
job is left queued and its worktree is removed and pruned, as on a baseline
failure. The next pass finds the deterministic announcement activity already
`done`, posts no second comment, and sets the marker then.

Output: `job.announced <job-id>` per delivered announcement, `job.started
<job-id>` per job, or `job.started none`; an undelivered announcement is
`announce failed <job-id>` on standard error. Neither line changes the exit
status.

### `agent runner work`

The detached worker for one job. `dispatch` starts it with the job worktree as
its working directory and resolves nothing relative to that checkout: `env` is
absolute, `PATH` keeps only absolute entries, `PYTHONHOME` and `PYTHONUSERBASE`
are unset, and the interpreter runs with `-P` and a `PYTHONPATH` that names
only the directory holding the installed `agent_cli` package, so an `agent_cli`
package or an executable shipped by the pull request is never imported or run;
an operator does not normally run it. It performs one `pr-review` job and
writes `state/logs/<id>.exit` last:

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
   `refs/remotes/origin/<base>...HEAD` with `--no-ext-diff` and `--no-textconv`,
   so repository configuration or attributes cannot run a command or replace
   the shown content. An empty diff fails, and a diff over 400,000 characters
   fails rather than being truncated: the lane always reviews exactly the change
   that was checked out.
5. Write the rendered prompt atomically to `state/logs/<id>.prompt`. Its fixed
   section marks everything below as repository data, fences the diff with a
   fence longer than any backtick run in it, and states the output contract:
   the review is the final message, ends with exactly `STATUS: complete` and
   either `RESULT: approved` or `RESULT: rejected`, and the lane must not run
   commands, contact GitHub or start other agents.
6. Start exactly one `reviewer` lane through `lane.launch` with model slot
   `first` (`ai-accounts.json`), `tmux=False`, the worktree as its directory and
   the agent home as its configuration home. `reviewer` is a read-only workflow
   kind, so a writable binding is refused. For the whole worker — from before
   the base-ref read through the review POST — a heartbeat appends
   `worker: worker running` to the transcript so the stall watchdog sees
   progress during network waits that grow no transcript.
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

A job that ends as failed gets one outcome comment describing how it ended:
`agent_failed`, `timeout`, `stalled`, `crashed` or `workspace_failed`. A
successful job gets none: its posted review is the output. The comment is a
`comment.post` activity that edits the job's existing status comment in place,
keeping the job to one status comment, and the timeout/stall sentence uses the
same budget the watchdog applied. A row carries `reported`
(`False` when the outcome comment is still owed, `True` after it is delivered)
and `report_attempts`. A failed comment increments `report_attempts` and is
retried on the next pass, up to five passes; after that the row is left alone.
A row written before these fields existed has no `reported` key and is never
reported. finalize marks a failed row `reported: False`; a done row is not
marked.

Output: `job.finalized <job-id>` (or `job.finalized none`),
`worktree.removed <job-id>`, `session.killed <name>` and `job.reported
<job-id>` per item, with failures reported as `reap failed <item>` and `report
failed <job-id>` on standard error. Any failed reap step makes the command exit
with status 1 so a schedule notices; a failed report does not change the exit
status because the next pass retries it.

This command is distinct from `agent supervise --session`, which watches one
ledger session.

### `agent runner prune`

Deletes finished queue rows and their log files once they are older than the
retention configured for their state. Each of `done` and `failed` is handled
independently; when a state has no retention setting, the command prints
`prune <state> not configured` and keeps everything in that state. Before the
row is deleted, its pending announcement and outcome activities are retired
with the same locked check `retry` uses, recorded as `superseded by pruning
the job`: the activities outlive the row, and a later scan could otherwise
post one after a re-ingested job's baseline and have `finalize` count it as
that job's output. A row whose status comment cannot be retired is kept with
its logs and worktree and reported as `prune skipped <job-id>: a status
comment could not be retired` on standard error. A leftover worktree under
`state/work/<id>` is removed after that with the same guards as `retry`; a row
whose worktree cannot be removed safely is likewise kept and reported as
`prune skipped <job-id>: <reason>`.
Output: `job.pruned <job-id>` per row, or `job.pruned none`.

### `agent runner retry JOB_ID`

Re-queues a job owned by this device, increments its attempt count, clears the
previous attempt's result fields, and removes its log files and leftover
worktree. The worktree is removed through `git worktree remove` and
`worktree prune`. Only if the directory still exists is a recursive delete
used, and only when the resolved path is strictly inside `state/work` and is
not a symlink; anything else is refused rather than deleted. Before the
worktree is touched, a status activity of the previous attempt that is still
`pending` is retired, so a later scan cannot post it after the new attempt's
baseline and have it counted as that attempt's output; a status comment that
cannot be retired refuses the retry and leaves the row unchanged.

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
worker's model selection and `lane_execution` declaration and reports an
allowed queue type that no catalogue skill carries, a catalogue skill with no
worker, a `default_skill_on_mention` that names no catalogue skill, and a
missing `prompts/pr-review.md` when an allowed queue type resolves to
`pr-review`. It requires the binaries `gh`, `git`,
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
- A schedule or service unit for the subcommands.
- Container packaging.
- The mergeable probe for `pr-mergeable` jobs; those jobs record
  `work_performed` as `unknown` until it is ported.
- @-mentions in a lane's review text are posted as written: the worker does
  not strip or resolve them.
- `scan_mentions` reads every unread notification and does not look at its
  `reason`; a job needs a comment that @-mentions the bot and passes admission,
  whatever the reason. The `review_requested` reason has no handling of its
  own.
- The rate limits (`max_jobs_per_actor_per_hour`,
  `max_jobs_global_per_hour`) are not ported.
- The replaced runner's public replies are not ported. Text that matches no
  catalogue pattern uses `default_skill_on_mention`; a request whose skill's
  queue type is not allowed is skipped without a reply.
- The decision log is not ported.
