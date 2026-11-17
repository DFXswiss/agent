# agent

This repository holds the agent work of DFX: the local session-store client `agent` (below), the legacy hub
`agent-core` ([further below](#agent-core-legacy-hub)) and the platform of agent.dfx.swiss (hub, web client, runner,
guest, contracts and tests; entry: [docs/platform/README.md](docs/platform/README.md)). The former repository
`DFXswiss/agent-core` was merged into this one with its complete history; every file kept its path.

Local session-store client. Record sessions, activities and (when a skill is attached) tasks on this machine, then pair the device to the [agent-core](#agent-core-legacy-hub) hub with GitHub.

Product decisions (visibility, pairing, sync, restore, what we will not build) are in [DESIGN.md](DESIGN.md). That file also locks the deterministic core: scripts execute, checks measure, gates decide, model text is never a transition, and the hub is not a coding control plane. A draft plus local tests is not a finished pull request; see [CONTRIBUTING.md](CONTRIBUTING.md). The frozen local-CI comment schema for private product repositories is [docs/local-ci-v1.md](docs/local-ci-v1.md) (`agent local-ci verify`). Production-error → draft pull request is the opt-in **error-fix** skill on this device, not the hub.

This device is the write owner of its own rows. The local store is PostgreSQL on `127.0.0.1`. `device.json` next to it is the device identity: wiping only the database must not mint a new device. The hub holds a full copy. `agent sync` pushes own events and pulls own catch-up, session-mail inbox snapshots, and person-ping snapshots. `agent restore` rebuilds a wiped database from the hub.

The [A38 standard](docs/a38.md) defines repository-owned local test requirements and author reports using the existing local-CI format. `agent a38` measures and validates reports; the [dfx pr guard](docs/a38-guard.md) explains repository rules and checks author comments without executing pull-request code. The detached job runner is documented in [docs/runner.md](docs/runner.md).

The planned team record (tasks, security issues and discussions with signed history and key-pair identity) is explained for the whole team in [docs/team-record.md](docs/team-record.md); the binding rules are DESIGN.md §24. How the team works through the hub (devices, handing work over, watching sessions, one overview) is explained in [docs/central-operation.md](docs/central-operation.md); the binding rules are DESIGN.md §25. The technical specification of device keys, levels, the allow list and grants is [docs/device-protocol.md](docs/device-protocol.md).

The required GitHub issue-to-PR workflow is defined in
[DESIGN.md §19.7](DESIGN.md#197-issue-assignment-to-human-merge), together with the
current implementation boundaries. A static script accepts the assignment and
confirms it in the issue before starting the implementation lane. Scripts own
all lane starts, tests, and GitHub communication; model lanes do not start
subagents, run tests, or access GitHub. The user works in GitHub and merges the
reviewed PR there. This is a workflow requirement, not a claim of a complete
deployed integration.

Model lanes run on an execution device, not inside the work device. The local
harness's lane starter starts them on an external lane host, where
`ai-accounts.json` declares
`lane_execution.mode = "this-device-is-lane-host"`. A work device may instead
declare `mode = "delegate"` with one operator argv; Agent prefixes every
headless model lane with that local foreground command, which runs it elsewhere.
Agent has no SSH/rsync remote-execution mode of its own; the operator command
does the copy and transport.

Model lanes perform work and return results or blockers. They never start
monitors, poll statuses, or wait for CI. Scripts own monitoring and inform a
model when an observed event provides useful work; see DESIGN.md §19.1.

Accounts and roles must start unconfigured (`NULL`); see
[DESIGN.md §19.8](DESIGN.md#198-configuration-starts-empty). Configure GitHub
execution accounts and session bindings explicitly in
[`github-accounts.json`](docs/github-accounts.md) for the executors that load
it. There is no default GitHub account or fallback to the host login on those
paths. A38 visibility lookup likewise requires an explicitly configured
`--github-session`, unless `--private` or `--public` supplies the report visibility.
[AI accounts and roles](docs/ai-accounts.md) configure provider profiles, models,
access and session bindings without built-in selections. Every driver those
selections name is described in the operator file `$AGENT_HOME/drivers.json`
([Drivers](docs/ai-accounts.md#drivers)); the repository ships no driver table,
and a missing or incomplete description fails loudly. Place `drivers.json` on a
device before upgrading it. `agent lane run` needs
`--session`; `agent run` uses its task session. Interactive session starts require
an interactive role binding whose driver has an `interactive` section. Automatic
period-usage reads are disabled until
`usage_session` is explicitly configured. Key-spend reads are
disabled until `usage_accounts` explicitly names one or more accounts whose
driver has a spend usage adapter. Window-usage reads stay disabled until
`usage_accounts` names an account whose driver has a windows usage adapter, and
rpc-usage reads until it names one whose driver has an rpc usage adapter. The optional
`ai-subscriptions.json` inventory lists every known subscription on the
dashboard's Subscriptions page.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
agent init
```

`agent init` starts a local PostgreSQL cluster (needs `initdb`/`pg_ctl` on `PATH`, or `AGENT_PG_BIN`). `AGENT_PG_DSN` may point at an existing loopback server instead. Data lives in `$AGENT_HOME` or, if that is unset, `~/.local/share/agent`. Identity is `device.json` next to the cluster. It also installs and starts the user-service device daemon (`agent daemon`), which starts knock, the local dashboard, and cli-bridge immediately; daemon `sync --follow` starts only after `agent pair`, once `device.json` has token and hub URL (init-before-pair remains valid). Only `agent init` creates that cluster. Commands that open the store start it again if it is stopped and fail with `run agent init` when it does not exist; they never run `initdb`. `agent pg status` reports the cluster without starting it, `agent pg stop` stops it (refused while a device daemon installed for this `AGENT_HOME` exists, because that daemon would start it again), and `agent daemon --uninstall` stops it together with the user service. `agent daemon --uninstall` refuses to run under a different `AGENT_HOME` than the one the service was installed for. Both commands stop with an error when a service unit exists but cannot be read or records no `AGENT_HOME`. When `AGENT_PG_DSN` is set, `agent init` writes it into the user service so the daemon uses the same server.

On a headless host managed by an operator-provided system supervisor, use
`agent init --no-service`. It creates the same store and device identity but
writes and starts no user service. Its output gives the exact `agent daemon`
argv and required `AGENT_HOME`, plus `AGENT_PG_BIN` or `AGENT_PG_DSN` when
configured, for a system service such as a launchd LaunchDaemon with `UserName`.
For `AGENT_PG_DSN`, the output only says that the variable is set and must keep
its current value; it never prints the DSN itself. `--no-service` is refused while a
device daemon user service is installed for the same `AGENT_HOME` (run
`agent daemon --uninstall` first), and it stops with an error when an existing
unit cannot be read or records no `AGENT_HOME`.

## Pair and sync

```bash
agent pair --hub https://agent.example
# confirm in the browser after GitHub sign-in
agent sync
agent assign --no-deviation --agent LOGIN --repo OWNER/NAME --title TEXT --body TEXT
agent sync --follow   # foreground WebSocket; reconnects with backoff on a dropped connection (cap 30s, logs to stderr; not a silent poll; process stays up)
agent restore   # after a wiped laptop; also works if leftover ledger.sqlite is present
# other commands refuse until you move ledger.sqlite aside
```

`AGENT_HUB` may replace `--hub`. There is no default hub URL. After `agent pair`, once `device.json` has token and hub URL, the user-service daemon starts `sync --follow`; a one-shot `agent sync` after pairing is enough for catch-up.
`agent assign` uses the paired device credential to ask the hub to create and
verify an agent-assigned GitHub issue. Use `--body-file PATH` instead of
`--body TEXT` for longer mandates. The required `--no-deviation` flag records
the owner's explicit statement that no deviation is authorized; deviations
instead need an explicit grant on the issue. The hub authorizes the human owner
and records `assignment.created`; the client never prints the device token,
and hub failure detail or recovery URLs redact the exact bearer even when each
character is literal or percent-encoded in a mixed representation. A literal
`%` in the bearer that itself starts a valid `%HH` next to encoded neighbours
in the same run is read decoded, so `a%41` in `%61%41` is not matched. Terminal
controls that appear only after that one percent-decode are removed by the
same control views. One
consecutive run of removable terminal controls between bearer characters has
no per-control count limit. A control's introducer, parameter, intermediate,
final, or terminator byte counts as a secret character only as far as a single
view yields it. An occurrence
that needs different treatment of two controls is not matched, for example one
control's final byte used as a secret character while another complete control
between later secret characters is skipped (`/opt/\x1b[31m\x1b[0my profile`
for `/opt/my profile`), or one control's introducer and final both used with
parameters between them (`Q[w` in `Q\x1b[32!w`). Such text never renders the
secret on a terminal and does not contain it contiguously in raw form; matching
every per-control combination would need a non-linear matcher. A response
counts as a mandate only with a positive issue number and a
matching HTTPS GitHub issue URL.

## Key identity

```bash
agent key create --id ID                                  # Ed25519 key in $AGENT_HOME/keys/ID.key; prints only the public key for the keys file
agent key list                                            # ids and public keys under $AGENT_HOME/keys
agent key pin-hub --id HUB_ID --public-key VALUE [--replace] # pins the hub key in $AGENT_HOME/hub-key.json
agent key whoami --id ID                                  # signed GET /api/key/whoami to the configured hub; prints the verified answer
agent key release --id ID --repo OWNER/NAME --pr NUMBER   # signed GET /api/key/release; may the pull request leave draft? exit 0 yes, 3 no, 1 error
```

No key exists until `agent key create` runs. The key directory has mode `0700`, every key file and `hub-key.json` mode `0600`; an existing key file is never overwritten, and a pinned hub key is replaced only with `--replace`. `agent key whoami` uses the hub URL stored by `agent pair`, stops with `hub key is not pinned` before sending anything when no hub key is pinned, never follows a redirect, and prints the body only when the pinned hub key signed the answer to this exact request within 120 seconds of this device's clock. Otherwise it reports `hub answer not verified`; for an unsigned refusal it adds the plain `error` value of the body, marked as unverified.

`agent key release` asks the hub whether one pull request may leave draft, for example `agent key release --id example-ci-1 --repo example-org/example-repo --pr 42`. It checks `--repo` (lowercase `owner/name` by the rule of the keys file) and `--pr` (1 to 2147483647, without sign or leading zero) before anything is sent, then sends the same signed request and verifies the answer exactly as `agent key whoami` does; the signed request target is `/api/key/release?repository=OWNER%2FNAME&pull_request=NUMBER`. A verified answer is used only when it has exactly the fields `repository`, `pull_request`, `released`, `failed` and `hub_key`, each once and in this order, names the asked repository and pull request and the pinned hub key id, and has `failed` `null` exactly when `released` is true and otherwise `task_missing`, `task_not_ready` or `question_unanswered`; any other answer is refused as `hub answer has an unexpected form` and never counts as a release. The verified answer is printed as one line of JSON with sorted keys. Exit codes for external programs: `0` the pull request may leave draft; `3` it may not (stdout still carries the answer, stderr one line `not released: <failed>`); `1` every error, including a signed refusal of the hub (`hub GET <target> → HTTP <status>: <error>`) and an answer that does not verify. Only a `service` key with the scope `release:query` and the repository among its subjects gets an answer.

## Record work

```bash
agent session register --id <session-id> --kind human
agent skills path
agent session skill attach --id <session-id> --skill spine
agent session skill attach --id <session-id> --skill review-loop
agent session skill attach --id <session-id> --skill pr-review
# error-fix belongs on the runner session that owns production-error work:
# agent session register --id <session-id> --kind runner
# agent session skill attach --id <session-id> --skill error-fix
# (also attach spine, review-loop, and pr-review on that same runner)
agent session start --id <session-id> [--provider DRIVER] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session input --id <session-id> --data TEXT
agent session input --id <session-id> --key enter|ctrl-c|tab
agent session keep-working --id <session-id> [--once|--follow]
agent session stop --id <session-id>
agent activity add --session <session-id> --type message --payload-file ./mail.json
agent task create --session <session-id> --workflow implement --title "…"
agent task pr --id <task-id> --repo <owner/name> --pr <number>
agent task close-from-pr (--id <task-id> | --session <session-id>)
agent round start --task <uuid>
# --vendor comes from the task pin, defaulted from ai-accounts.json at task creation;
# implementer may be separate, while review providers come from models. To use another
# provider for a slot, change the pin explicitly first:
agent task models --id <task-id> --slot first|second|implementer --vendor <provider> --reason "<why>"
# The change is recorded on the task; earlier rows keep their vendor.
# If the implementer cannot run, run agent agent finish --verdict blocked for its
# row (the task becomes failed), change the implementer pin, then run round start.
# If a PR reviewer cannot run, run agent agent finish --verdict unavailable for its
# row, then agent gate record --verdict unavailable, then close-step --status
# unavailable, then change the pin. Never substitute silently or automatically.
# agent task models refuses done, merged and superseded tasks, and coordinator tasks.
agent agent start --session <session-id> --task <uuid> --role implementer --vendor <implementer-provider> --round 1
agent agent finish --id <implementer-uuid> --verdict done
agent agent start --session <session-id> --task <uuid> --role pr-reviewer-quality --stage review-1 --vendor <first-slot-provider>
agent agent finish --id <reviewer-uuid> --verdict approved
agent check record --task <uuid> --name lint --command "pytest" --result pass
agent gate record --task <uuid> --stage review-1 --dimension quality --vendor <first-slot-provider> \
  --verdict approved --head <sha> --agent <reviewer-uuid>
agent work add --session <session-id> --key standing --closable-by human
agent work set --session <session-id> --key standing --status done --source human --actor-session <session-id>
agent work list --session <session-id>
agent models [first|second|implementer] [--task <uuid>] [--json]   # device setting, or the pin of a task
agent activity add --session OWN_SID --type message --payload-file ./second-review-mail.json
agent checklist set --task <uuid> --key spec_written --status ja --source human --evidence "spec.md"
agent allow --action claim-done|pr-ready|pr-create|task-done [--session ID] [--task <uuid>] [--draft true|false] [--json]
agent next --task <uuid>
agent close-step --task <uuid> --key KEY --source script|human|runner --evidence TEXT [--status ja|n_a|unavailable]
agent small-change --task <uuid> [--repo PATH] [--json]   # exit 0 small, 2 not small
agent run --task <uuid> [--dry-run] [--head SHA] [--cwd PATH] [--spec-file PATH] [--no-tmux]
agent github pending
agent query --match-file PATH
agent subscribe list|set --file PATH|clear
agent mail pending|ingest
agent ping send --to some-login --kind review-request --task <uuid> --note "ready"
agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]
agent status
agent dashboard  # local dashboard with the orders view; 127.0.0.1 only
agent cli-bridge
agent pg status
agent pg stop
```

For an external A38 second review, `second-review-mail.json` is session mail:
its outer object has `to_session`, and its `payload` is the strict
`a38.second-review/v1` result documented in
[Issue coordinator — A38 mode](docs/issue-coordinator.md#a38-mode).

`agent run` records a local check when `local_check_pass` is open and the snapshot has no local checks yet (it does not rerun an existing failed check). It closes an agent step when the session store already has the artifact, and with `--spec-file` launches the vendor lane (tmux by default; `--no-tmux` for a subprocess). When `pushed` is open it git-pushes (no force) and closes with the HEAD sha; when `mergeable` is open it measures GitHub mergeability and checks and closes only if both are green. Reviewer lanes are not auto-approved from `STATUS: complete`. An implementer lane closes the round only with exactly one valid `STATUS` record and exactly one role-valid `RESULT: done|no-change|blocked` record. Every `STATUS:` and `RESULT:` line counts before its value is validated, so a malformed extra record also aborts the row and leaves the round retryable. Failed-lane notes and printed diagnostics are bounded and secret-redacted. If standalone `agent lane run` cannot write its audit row, it preserves the lane verdict and prints only the fixed `lane.run audit write failed` category without backend exception text.

`agent small-change` measures one implement task against the attached pull request: it takes the merge base with the attached pull request's current base commit, read from GitHub when the command runs (the pull request is attached with `agent task pr`), requires local HEAD to equal the pull request head, counts paths after `--no-renames`, and accepts at most 30 changed lines in at most 3 files with no binary file. Built-in path classes (workflows, dependencies, infrastructure, migrations, security and money, review rules) are always excluded; `.github/small-change.json` on the base commit may add more. The verdict is recorded on the task together with the base branch, and closing from the merged pull request requires the same base branch. `implementer_done` and `reviewer_approved` accept the evidence `small change at <sha>` only after a `small` verdict at that sha, and `done` needs the small verdict at the head of the four PR gates, which stay required. On that route `done` comes only from `agent task close-from-pr`, which also requires the merged pull request's merge base to equal the measured one.

`agent github pending` is one scan: owned pending `pr.open`, `comment.post`, `review.post`, and `issue.write` rows via `gh`. Pull requests are drafts. A retry reuses an existing open draft, issue, or comment instead of creating a second one. A `comment.post` payload may carry the runner's status `edit_marker` (`<!-- agent-runner-job:<job id>:<UTC timestamp> -->`, contained in its body, without `exact_body`): the executor then edits the newest own comment that contains it instead of posting a new one. That marker is the only `edit_marker` accepted, so a lane cannot use it to overwrite another bot comment.

This package **is** the runtime: install it locally and run `agent`. There is no second store binary. The packaged files under `src/agent_cli/skills/` **are** the skill contracts (`spine`, `review-loop`, `pr-review`, `error-fix`, `conflicts`). `agent skills path` prints that directory, or `AGENT_SKILLS_DIR` when set. Operator-specific git or deploy rules stay outside this package.

Kind is `human`, `runner`, or `other`. Skills are opt-in (`spine`, `review-loop`, `pr-review`, `error-fix`, `conflicts`). Without `spine`, task/checklist/round/work/check/`next`/`close-step`/`run` commands refuse. `allow` uses `spine` when it loads a session or task. Without `review-loop`, implementer and reviewer `agent agent` commands refuse. Without `pr-review`, `agent gate` and pr-reviewer `agent agent` commands refuse. Without `error-fix` the production-error loop does not run. Without `conflicts`, `agent conflicts` refuses. `AGENT_SKILLS_DIR` may override the packaged skill directory only when that directory contains `spine/SKILL.md`, `review-loop/SKILL.md`, and `pr-review/SKILL.md`; otherwise the command fails. `error-fix` and `conflicts` ship in the packaged tree; an override need not copy them. Unset `AGENT_SKILLS_DIR` and run `agent skills path` again to print the packaged directory.

Session mail, `pr.merged`, and `error.seen` notify channel `agent_inbox`. `issue.assigned` does not: `agent watch assigned` knocks the queue head after writing `MANDATE.md` / `QUEUE.md`. The knock text is only `da ist Post id <uuid>`:

```bash
agent knock --once
agent knock             # valid foreground loop; the user-service daemon is the supported always-on path
agent watch pr-merged   # one scan; needs GitHub CLI (`gh`); the device daemon covers the loop
agent watch pending     # one scan; runs subscription.set and query.request against the hub
agent watch session-usage # one scan of the usage session's period usage into usage.snapshot
agent watch account-usage # one scan of the configured usage accounts' key USD usage
agent watch window-usage # one scan of configured window usage (5h, weekly, per model; windows and rpc adapters)
agent watch token-keepalive # one keep-alive pass over window-usage accounts; needs token_keepalive
agent account-login ACCOUNT --email ADDRESS [--no-mailbox] # sign a subscription into the account's profile
agent coordinate --session ID [--follow] # explicit static issue workflow; coordinator.json
agent order submit|list|show|events|wait|ack|reply|override|cancel|done|report  # local orders for the coordinator; coordinator.json "orders": true
agent conflicts --session ID [--once|--follow] [--dry-run] # comment-mandated conflict resolution; conflicts.json
agent watch assigned [--follow]  # legacy assignments; needs `gh` and `$AGENT_HOME/watch.json`
agent watch errors      # one scan; $AGENT_HOME/error-fix.json; no log host in this package
agent watch error-fix   # one scan; find-or-create implement task + isolated worktree
agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]
# agent knock (daemon, no --once) polls configured usage sources, pending, pr.merged, github pending, mail pending, errors, and error-fix every 60s; each window-usage and rpc-usage account no more often than its configured poll_seconds; token keep-alive (at most every 5 minutes, when `token_keepalive` is set)
```

The token keep-alive and `agent account-login` are described in [docs/ai-accounts.md](docs/ai-accounts.md#token-keep-alive).

`agent supervise` posts a short status line to Telegram when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the environment. The follow CLI does not ask closed questions. Working vs not working for paging is whether the interactive driver's tmux session exists: it posts `not working` only when that session is gone, not when the prompt is idle between turns. The pane working probe (the driver's `interactive.pane.working` and `queued` markers in `drivers.json`) is for the follow loop, not for Telegram. A send failure is printed to stderr and does not stop the loop. Credentials stay out of git.

The optional [issue coordinator](docs/issue-coordinator.md) uses explicitly
configured worker sessions, GitHub and AI accounts, checks, and workspace roots.
Installation starts with no configured worker. `agent coordinate --session ID`
advances one worker; `--follow` lets the script observe later events. The daemon
starts configured workers on startup; changing its worker set requires a restart.
Restarting the daemon or `agent coordinate --follow` leaves a running model lane
in place once its process record is durable. A stop before that record is written
ends the lane. The next coordinator reads that lane's saved process id, start time,
and durable stdout and stderr, waits for the lane, and records its result once.
A lane that finished in the gap is recorded from those files. `lane_timeout`
still runs from the original start, and cancelling an order still kills the
lane. Check and test jobs still stop when their coordinator stops.
Workers may select explicit repositories, approved GitHub owners, or both;
explicit entries win. They may require matching hub-issued assignments before
acceptance.
A worker with `"orders": true` also accepts orders submitted on the device with
`agent order submit` or in the dashboard. A front end learns about questions
and results through `agent order wait` or `agent order events`; the coordinator
does not depend on any front end.
An order shows `running` only while a lane or check job of that order is
running, or while the next step of its current phase is runnable because none
of that step's checks is recorded as `nein`. It shows `waiting` when that next
step is held by a check recorded as `nein`, including while the stored phase
is still the resumed phase and the question has not been raised again.
`agent order list`, `agent order show`, and the dashboard then name that
check, for example `waiting implement (round_budget)`. Phases `ask` and
`blocked` also show `waiting`.
Idle workers on one device share GitHub requests for one login. Assigned-issue
discovery runs once per repository per `poll_seconds`. When the stored listing
is one page and shorter than `per_page`, the next request sends that page's
ETag as `If-None-Match`. An unchanged page is a 304 and does not consume the
5000-request hourly REST budget. A full page or a later page is read again in
full at the same interval, still once per repository. With `poll_seconds` 30
and three repositories, discovery is 360 requests per hour for that login no
matter how many idle workers share the account. `gh api user` is shared by
those workers and by `agent knock` for at most 15 minutes per account.
Busy workers, order pickup, and the pause between ticks stay the same. This
budget is the idle discovery and login traffic, not pushes or comments from a
busy worker.
Legacy `watch assigned` dispatch and `supervise` refuse those worker sessions.

The optional [comment mandate `conflicts`](docs/conflict-mandate.md) worker is
separately configured in `$AGENT_HOME/conflicts.json`. A human comment
`@<worker-login> conflicts` on an open pull request makes the static script rebase
the branch, start a confined implementer lane only for content conflicts, run
the configured checks, and push with `--force-with-lease`. Absent or empty
configuration enables no worker; installation selects no account, provider,
repository, or check command.

The error-fix executor find-or-creates the implement task and isolated worktree; `agent github pending` still opens draft pull requests.

`agent watch session-usage` uses the existing login token that the usage session's driver keeps in its credential file (the driver's `period` usage adapter), does not start an interactive session, and does not knock the TUI. Each `usage.snapshot` includes the account email, provider, and subscription tier. Under the device daemon, the knock child records those snapshots (and scans pending, `pr.merged`, github pending, mail pending, errors when `$AGENT_HOME/error-fix.json` exists, and pending `error.fix`) on the same interval. `agent daemon --install` / `--uninstall` manage the user service; `agent init` already installs and starts it.

`agent watch account-usage` reads only explicitly selected `usage_accounts`
and stores changed USD usage and limits from their driver's `spend` usage
adapter. `agent watch window-usage` reads the login of each selected account
whose driver has a `windows` usage adapter and stores changed five-hour, weekly
and per-model weekly percentages. In the same scan it asks the driver's own
local JSON-RPC server for each selected account whose driver has an `rpc` usage
adapter and stores the same windows; Agent sends no token for it and stores
none, and reads the account file only to decode the identity claim of the ID
token, without a signature check;
see [AI accounts](docs/ai-accounts.md). Lane
processes record a script-owned `lane.run` activity after completion, including
reported tokens, a bounded redacted file-tool transcript, and bounded final
text. Tmux-backed lane captures join soft-wrapped lines before diagnostics are
redacted, printed, or persisted in that evidence. Raw and resolved provider
config paths are matched across interleaved terminal controls when one fixed
view yields the path, before sanitization, and are hidden again afterward.
The linear view matcher has no control-run limit. Lane diagnostics
(`lane.redact_lane_text` / `hide_config_dir`) cover the configured config
directory. The gh config directory and signing key are covered in GitHub
activity execution errors (`github_act._safe_execution_error`). Coordinator
path roots (the control dir, the worktree, the workspace, the agent home, and
the user home) are covered via `replace_path_roots`. The device bearer is
covered in hub assignment text. The views
are the raw text, text that drops complete string controls, complete ESC/CSI
sequences, and C0/C1 bytes, text with only
introducers, terminators, and C0/C1 bytes removed, and text with complete
ESC/CSI sequences removed while string-control payloads stay. One further view
keeps an ESC final that also introduces a string control and drops `ESC \`.
Another keeps every escape final, including the `\` of `ESC \`, while dropping
introducers, CSI parameters and intermediates, ESC intermediates, and C0/C1
bytes, and it keeps string payloads. Another keeps only each control's
introducer byte. Another keeps only each control's final or string terminator
and drops string payloads. Each view is searched
with KMP, overlaps included. Only the device bearer additionally gets the
percent-decoded views. Percent-tolerant matching of that bearer also searches a
percent-decoded copy of each view. The raw bearer text is percent-decoded once,
and when that decoded text contains a terminal control the same control views
are built on it and searched for the literal bearer. Their maps compose through
the decoded text back to raw spans, and those views are not percent-decoded
again. Consecutive `%HH` triplets (either hex case)
are one UTF-8 byte stream, decoded in a single pass: a valid sequence becomes
one character mapped to the raw span of all of its triplets, and an invalid or
truncated sequence falls back to one character per byte so those bytes are not
merged into a character they do not encode. An ASCII `%HH` stays one character
spanning its three-character raw span, and every other character is copied.
The literal bearer is matched on the undecoded view
and on that decoded view, and on the control views of the once-decoded raw
text. The union of the mapped spans is redacted. Raw,
fully encoded (`%25` for `%`), and mixed per-character forms of the bearer are
covered. A literal `%` in the bearer that itself starts a valid `%HH` next to
encoded neighbours in the same run is read decoded, so `a%41` in `%61%41` is
not matched. The bearer, including one that contains `%`, takes time linear in
the view length plus the bearer length. A control's introducer, parameter,
intermediate, final, or terminator byte counts as a secret character only
as far as a single view yields it. An occurrence that needs different
treatment of two controls is not
matched, for example one control's final byte used as a secret character
while another complete control between later secret characters is skipped
(`/opt/\x1b[31m\x1b[0my profile` for `/opt/my profile`), or one control's
introducer and final both used with parameters between them (`Q[w` in
`Q\x1b[32!w`). Such text never renders the secret on a terminal and does not
contain it contiguously in raw form; matching every per-control combination
would need a non-linear matcher. Both configured and
provider-reported model strings are bounded and redacted. Neither activity
stores argv, environment, profile paths, or
credentials; a tool target outside the worktree is stored as `[redacted]`,
including a Glob pattern or an absolute or relative-traversal path fragment
inside a Grep pattern, also when assignment text or punctuation precedes it.
Numeric and named backslash escapes (`\x`, `\u`, `\U`, `\N{`, octal, and
`\c`) fail closed by redacting the complete target. A `%HH` percent escape in a
path or pattern target also redacts the complete target before normalization,
without decoding. Any raw ESC or C0/C1
control byte does the same before path classification.
The shared text redactor performs one initial ESC/C1 search. For a raw logical
line that contains ESC/C1, a compiled case-insensitive candidate check
runs over a control-byte-translated view. Only candidate lines build three
primary views, one at a time: a control-stripped view
(complete or unterminated controls and payloads removed), a payload-kept view
(the outer introducer, terminator, and CSI parameter bytes removed), and a key-join
view. The key-join view uses the assignment scanner's own classifier to retain
only control-sequence bytes that the scanner may join into a key, including a
key-character CSI/ESC final and a `-` intermediate, while dropping string-control
payloads and all other control bytes. If any view contains a shared secret
keyword, case-insensitively, the raw logical line is redacted
from the start of the mapped whitespace token through logical-line end.
Standalone removable C0 bytes, including backspace, NUL, DEL, and carriage
return, are removed from every structural view but do not activate the rule
without ESC/C1. Nested string controls keep their payload text at every nesting
depth; their introducers and terminators are omitted. Retained string-control
payloads are checked in both a payload-clean view and a payload-key-join view.
The payload-clean view omits complete nested CSI spans (parameters,
intermediates, and final), other ESC sequences, C0 bytes, and lone C1 bytes. The
payload-key-join view contributes exactly the key characters from nested
CSI/ESC sequences that the assignment scanner may join into a key and omits
all other control bytes. The second view is built lazily only for payloads that
contain nested controls, and a keyword in either view activates the line rule.
An unterminated nested string control fails closed and treats the whole logical
line as matched. Complete and unterminated string-control spans are indexed on
the full raw input before lines are formed, so payload newlines are not
boundaries; a mapped start inside such a span moves to its introducer. Following
normal lines outside the span remain unchanged. An unterminated string control
extends through the end of the text and swallows every following physical line
into its payload. Lines without ESC/C1 keep the precise scanner
unchanged, and controlled lines without a keyword retain the existing control
handling.
A whitespace token exactly `<config_dir>/` followed only by
`[A-Za-z0-9._-]+` is the one structural exception: keywords inside that
placeholder do not trigger the line rule. Any other byte in that token, or a
keyword elsewhere on the line, still triggers it. Lane sinks replace raw and
control-interleaved config-directory paths on the complete source before this
structural rule, apply their length bound next, and run precise redaction last.
Credential assignments use one scanner over complete, unbounded key tokens,
including quoted JSON keys. It is linear, except where whitespace follows
terminal controls that hold key characters (a key character (a letter, a digit,
`_` or `-`) among the parameter, intermediate or final bytes of a CSI or ESC
sequence, or a key character in a string-control payload); such
text costs time quadratic in the number of those controls. Quoted values are consumed through their
closing quote or the bounded input end. The scanner recognizes the legacy
`passwd` and `access_key` / `access-key` / `accesskey` key forms and consumes
removable terminal controls between the key, assignment operator, and value.
After a complete secret keyword, the scanner skips any following whitespace and
terminal controls. A control ends an unquoted key only when an operator occurs
before another key character: a visible `=` or `:` wins, and only when none is
visible may a complete skipped string-control span supply its payload operator.
Otherwise the control stays inside the sticky key, so suffixes such as `_file`
and `suffix` remain part of the secret key. A whitespace token that contained a
string-control span and contains a secret keyword is redacted whole as a
fail-closed defence. Before
the key is complete, payload operators stay opaque so split key fragments still
join. BEL terminates OSC only; DCS, PM, APC, and SOS continue through BEL to
`ESC \` or the eight-bit ST byte.
The bounded control-run window of at most 64 runs in
`TERMINAL_INTERLEAVE_PATTERN` applies only to the raw regex consumers
`_RAW_AUTH_HEADER` (authorization, set-cookie, and cookie headers),
`_RAW_BEARER` (generic Bearer tokens), `_RAW_KNOWN_TOKEN` (`gh[pousr]_`,
`github_pat_`, and `sk-` tokens), `_RAW_AKIA` (`AKIA` plus 16 of `0-9A-Z`),
`_RAW_PRIVATE_PATH` (`/Users/` and `/home/` paths), `_RAW_URL_AUTH`
(control-tolerant URL schemes), and `_RAW_PRIVATE_KEY` (private-key header
words). CSI/ESC parameter and intermediate bytes are matched with a fixed
window of at most 64 bytes each (`_MAX_ESCAPE_PARAMETER_BYTES`). The linear
view matcher has no control-run limit. Lane diagnostics
(`lane.redact_lane_text` / `hide_config_dir`) cover the configured config
directory. The gh config directory and signing key are covered in GitHub
activity execution errors (`github_act._safe_execution_error`). Coordinator
path roots (the control dir, the worktree, the workspace, the agent home, and
the user home) are covered via `replace_path_roots`. The device bearer is
covered in hub assignment text. Only the device bearer
additionally gets the percent-decoded views. That matcher searches a fixed set
of views with a linear matcher, including truncated CSI/ESC prefixes and
string-control payloads. An occurrence that needs different treatment of two
controls is not matched. For bounded
lane fields, the controlled-line structural rule
first sees the complete raw source; the result is then cut to the limit plus a
small fail-closed inspection margin before the remaining precise redaction.
Lane processes do not inherit ambient proxy or CA settings (`HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`,
`SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`), and a usage adapter
with `proxy_from_environment: false` ignores them too. The windows usage
adapter builds its client with the adapter's `proxy_from_environment` setting as well.

`agent watch assigned` reads `$AGENT_HOME/watch.json`:

```json
{ "assigned_repos": ["Owner/repo"], "session_id": "assigned" }
```

Missing or empty `assigned_repos` is an error. `session_id` is optional, defaults to `assigned`, and may contain only `A-Za-z0-9_-`. A session already present under that id must be `kind=runner`. The auto-created runner session attaches `spine`, `review-loop`, and `pr-review` (those skills stay opt-in for every other session). The working directory is `$AGENT_HOME/sessions/<session_id>` unless `AGENT_SESSION_ROOT` is set. The first successful scan records the `assigned_watch_since` watermark and the assigned session id, and creates no activities. Changing `session_id` after that pin is an error. The scan uses the session account from `github-accounts.json`; a missing binding or a `gh api user` mismatch is an error. Hub pairing is separate and still used for sync. Later scans enqueue `issue.assigned` on **that one** runner session, push to the hub, write `MANDATE.md` / `QUEUE.md`, and start the interactive driver only if that session is not already attached. The insert does not notify the knock daemon. There is one terminal; further assignments wait in the knock queue until the supervise script records `issue.assigned.ack` with `payload.assigned_id` set to that activity id. The follow CLI does not ack from pane text. `MANDATE.md` lists session and activity ids. `QUEUE.md` lists ids and urls. Neither file contains issue bodies. Use `--follow` for a 30s loop, or cron for one-shot runs.

### Session terminal control

This device is the only place that starts, stops, or types into a live terminal. Control mutates the owned session row’s `runtime` fields (`tmux_session`, `control`, optional `cols`/`rows`). Foreign sessions stay read-only.

```bash
agent session start --id <session-id> [--cmd "bash -l"] [--cols 80] [--rows 24]
agent session start --id <session-id> --provider <driver>
# → started <id> tmux=agent-… interactive=<uuid>
# later starts resume that uuid; they do not reuse the store session id
agent session input --id <session-id> --data "ls\n"
agent session input --id <session-id> --key enter
agent session keep-working --id <session-id> --once
agent session stop --id <session-id>
# → stopped <id>
```

`agent session keep-working` is for a session whose standing job is already in the working directory. The interactive driver stops after each turn; this command does not interrupt an in-flight turn. A tool-approval modal is cleared with Enter. The first idle composer tick (caret visible) sends one standing instruction to keep going until the assignment is complete. Later idle ticks send only `Continue.`. Bare `keep-working` and `--once` are one tick; `--follow` polls every 30s. A missing tmux session is reported and left alone. An unreadable pane is not typed into.

`$AGENT_HOME/runtime-targets.json` may map a session id to an argv list prepended to every `tmux` call for that session. A missing key is local tmux, as today. The file stays on this device; it is not a hub event.

`agent cli-bridge` (started by the device daemon) accepts allowlisted store/spine argv on loopback. It does not run `session start`, `run`, GitHub, or mail. `python -m agent_cli.stub` is the matching client. Set `AGENT_CLI_BRIDGE=127.0.0.1:7846` on the client. The server bind is `127.0.0.1` or `::1` (`AGENT_CLI_BRIDGE_BIND`).

`agent sync --follow` announces `control-ready`, applies hub `control` frames on this device, acks them, and publishes `terminal` captures for owned sessions with `runtime.control=attached`. Tmux soft-wrapped lines are joined before the full capture first has its configured profile path replaced, then passes the controlled-logical-line rule, and then enters the precise raw secret scanner. That scanner classifies assignments while complete or unterminated OSC, DCS, PM, APC, and SOS spans remain opaque, replaces those spans with `[redacted]`, and applies raw-token redaction before terminal sanitization. The configured path replacement and precise redaction then run again on sanitized text; this publication path has no bounded diagnostic limit. Every remaining raw whitespace-delimited token containing an ESC/C1 sequence is redacted in place, whether that sequence is inside the token or at either boundary. The only exemption is otherwise escape-free visible text wrapped by at most one strict leading and/or trailing `ESC [` SGR sequence whose parameters contain only digits and semicolons; the parameter bytes and visible text must not match any applicable secret pattern. Known-secret matching on this publication path is the configured profile path (the config directory) on the linear view matcher's raw and control views. One view yields a CSI/ESC final that is the next secret character, a truncated prefix, or a string-control payload. A control's introducer, parameter, intermediate, final, or terminator byte counts as a secret character only as far as a single view yields it. An occurrence that needs different treatment of two controls is not matched, for example one control's final byte used as a secret character while another complete control between later secret characters is skipped (`/opt/\x1b[31m\x1b[0my profile` for `/opt/my profile`), or one control's introducer and final both used with parameters between them (`Q[w` in `Q\x1b[32!w`). Such text never renders the secret on a terminal and does not contain it contiguously in raw form; matching every per-control combination would need a non-linear matcher. Only the device bearer additionally gets the percent-decoded views. The line rule still requires ESC/C1 to activate, but all of its structural views remove standalone removable C0 bytes. Newlines inside string controls do not split logical lines, and a redaction boundary inside a string control moves to its introducer so its payload cannot outlive a separately replaced introducer. Sanitization preserves tabs and newlines, and terminal publication otherwise preserves captured whitespace; deduplication compares the complete sanitized capture. Terminal bytes are not store events.

One shared linear, nesting-aware scanner supplies every string-control span
boundary to redaction, terminal sanitization, logical-line splitting, and all
structural views, including both payload views; an unterminated outer span
reaches the end of the text.

For that raw assignment classification, the scanner skips whitespace and
terminal controls after a complete secret keyword. A control ends an unquoted
key only when a visible `=` or `:`, or otherwise a valid complete
string-control payload operator, occurs before another key character. Without
such an operator the control remains an opaque interleave in the sticky key. A
raw token containing both a string-control span and a secret keyword is
redacted whole before the span is replaced. If the key is not complete yet, the
payload operator remains opaque and later key fragments still join. BEL ends
only OSC; the other string controls require ST.

## Testing against another hub

Do not point `AGENT_HOME` at a scratch directory and run `agent init` there. That creates a second cluster and a second device identity, and the user service label is shared, so the daemon would be repointed at the scratch home. Use a throwaway database on the existing cluster instead:

```bash
CLUSTER_HOME=${AGENT_HOME:-"$HOME/.local/share/agent"}   # the home of the existing cluster
PORT=$(cat "$CLUSTER_HOME/pg/port")
psql -h 127.0.0.1 -p "$PORT" -U agent -d postgres -c 'CREATE DATABASE hubtest'
export AGENT_HOME=~/hubtest-home          # holds the second device.json only
export AGENT_PG_DSN="host=127.0.0.1 port=$PORT user=agent dbname=hubtest"
agent pair --hub https://hub.example      # no agent init
```

`AGENT_PG_DSN` bypasses the cluster logic entirely; `agent pg stop` refuses to run while it is set. Drop the database when you are done.

## Tests

A draft plus local tests is not a finished pull request. See CONTRIBUTING.md.

```bash
pytest
```

Tests start an ephemeral PostgreSQL cluster, or use `AGENT_TEST_PG` when that is set (CI). The integration test talks to `agent-core` when that package is importable (install it next to this checkout).

## License

MIT. See [LICENSE](LICENSE).

# agent-core (legacy hub)

Hub for the [agent](#agent) session store (former repository `DFXswiss/agent-core`, merged into this one).

Anyone can sign in with GitHub. Each person sees their own work. People listed on a team in `teams.yaml` also see that team. Add or remove members with a pull request.

The hub stores a full copy of every paired device. Sync is bidirectional: the device is the write owner of its own rows; the hub fans those events out and can restore a wiped device.

This repository does not describe a particular deployment environment. Run it anywhere that can reach GitHub OAuth and serve HTTPS.

Image builds on `develop` / `main` push `dfxswiss/agent-core:beta` / `:latest` and the git SHA. After a successful push they notify the configured infrastructure repo (`DISPATCH_TOKEN` + `DISPATCH_REPO`) so the running hub pulls the new tag. If those secrets are unset, the image is still published.

### Run locally

Create a GitHub OAuth App whose callback is `http://localhost:8787/auth/github/callback`. The hub requests scope `read:user repo` so people can sign in, the website can list open pull requests, and owners can create and answer agent assignment issues with their own token. After sign-in the website leads with open PRs from GitHub (`GET /api/prs`); usage, sessions, tasks, agents, pings, and devices come from the local hub replica. Then:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
export AGENT_CORE_PUBLIC_URL=http://localhost:8787
export AGENT_CORE_SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export AGENT_CORE_GITHUB_CLIENT_ID=...
export AGENT_CORE_GITHUB_CLIENT_SECRET=...
export AGENT_CORE_GITHUB_AUTHORIZE_URL=https://github.com/login/oauth/authorize
export AGENT_CORE_GITHUB_TOKEN_URL=https://github.com/login/oauth/access_token
export AGENT_CORE_GITHUB_USER_URL=https://api.github.com/user
export AGENT_CORE_DATABASE=./hub.sqlite
export AGENT_CORE_TEAMS="$(pwd)/teams.yaml"
export AGENT_CORE_KEYS="$(pwd)/keys.yaml"
export AGENT_CORE_SIGNING_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export AGENT_CORE_HUB_KEY_ID=hub-local-1
export AGENT_CORE_HOST=127.0.0.1
export AGENT_CORE_PORT=8787
export AGENT_CORE_RECORD_DATABASE="postgresql://record_writer:<64 hex>@127.0.0.1:5432/record?sslmode=disable"
python -m agent_core
```

Before the first start, create the record database and its two roles on a PostgreSQL 16 server
(RECORD-PROTOCOL.md section 11.2) and run `python -m agent_core.record migrate` with
`AGENT_CORE_RECORD_OWNER_DATABASE` and `AGENT_CORE_RECORD_DATABASE` (see below). Unset
`AGENT_CORE_RECORD_OWNER_DATABASE` again before you start the hub.

Open http://localhost:8787 and sign in.

Every variable above is required. The process exits if one is missing.

- `AGENT_CORE_PUBLIC_URL` has no user information, path, query or fragment; a single trailing `/` is allowed. An international host name is written in its ASCII form (`xn--…`). Its host is a name, not an IP address, and does not end with a dot, because it is the relying-party id of the passkeys (PASSKEY-PROTOCOL.md section 3). Otherwise the process exits. `AGENT_CORE_HOST` is the address the process listens on, not the public URL, and may stay `127.0.0.1`.
- `AGENT_CORE_KEYS` is the path of the keys file (`keys.yaml` next to `teams.yaml`).
- `AGENT_CORE_SIGNING_KEY` is the 32-byte seed of the hub's Ed25519 signing key as 64 lowercase hex characters. It is a secret: every environment has its own seed, and the hub never logs or returns it.
- `AGENT_CORE_HUB_KEY_ID` is the id of the hub's signing key (lowercase letters, digits and `-`, 3 to 63 characters, starting and ending with a letter or digit). It is not a secret.

- `AGENT_CORE_RECORD_DATABASE` is the connection string of the writer role of the team record:
  `postgresql://<role>:<password>@<host>[:<port>]/<database>?sslmode=<mode>`, with a password of
  64 lowercase hex characters and `sslmode` `disable` (only for a loopback address or
  `localhost`), `require` or `verify-full` (RECORD-PROTOCOL.md section 15). It is a secret. The
  hub refuses to start when `AGENT_CORE_RECORD_OWNER_DATABASE` is set in its environment, also
  when it is empty.

At start the hub logs one line `hub key <id> <public key>`; callers pin that pair. Before it
serves, it checks the record database and verifies the whole record (RECORD-PROTOCOL.md section
11.4) and logs `record verified <position> <record hash>` (`record verified 0 -` for an empty
record). When that start fails it logs `record start failed <step>`, with `<step>` one of
`config`, `schema`, `writer`, `verify` and `verify_read`, and exits; `record start failed verify`
always follows a line `record verify failed <position> <failure>` that names the failure.

### Teams

Edit `teams.yaml` and open a pull request:

```yaml
teams:
  dfx:
    members:
      - your-github-login
      - agent-github-login

agents:
  agent-github-login:
    owners:
      - your-github-login
```

Logins are compared in lowercase. A login that appears on no team still signs in and still syncs; they only see themselves.

The optional `agents` map declares GitHub logins operated by autonomous agents. An agent must be a team member, and each owner must share at least one team with that agent. Owners can pair a device as the agent from the website, control its sessions, create GitHub issue assignments, and reply to assignment comments. An agent cannot own itself, duplicate owners and any key written twice in the file are rejected, and a malformed roster fails closed at startup.

### Protocol

See [PROTOCOL.md](PROTOCOL.md).

The wire format of key identity is in [KEY-PROTOCOL.md](KEY-PROTOCOL.md). The hub side of its first build step is built: the keys file `keys.yaml` (changed only by pull request), signed requests under `/api/key/`, the hub's signed answers, and `GET /api/key/whoami`.

Sign-in with passkeys, invites and the certified session key are specified in [PASSKEY-PROTOCOL.md](PASSKEY-PROTOCOL.md). The hub side of that step is built in `agent_core.passkey`: the verification library (encodings, passkey responses, registrations, the statement, `verify_certificate` and the command text, with the shared test vectors in `tests/vectors/passkey.json`), the passkey tables in the hub database, the JSON routes `POST /auth/invite/open`, `POST /auth/invite/finish`, `POST /auth/passkey/begin` and `POST /auth/passkey/finish`, signed requests by a session key, `GET` and `POST /api/key/invites`, `POST /api/key/session/end` and `POST /api/key/commands`. The website pages `/passkey` and `/invite` are not built yet.

The operator commands of the passkey step run on the hub host with the same environment variables and database as the hub; they never start it:

| Command | Does |
|---|---|
| `python -m agent_core invite-bootstrap --handle HANDLE --team TEAM --seconds N` | Creates the bootstrap invite for the administrator's first passkey, only while no keys-file entry holds `keys:invite` and no other bootstrap invite is open. Prints only the link to stdout and `invite bootstrap created <invite id> <handle> <team> <expires>` to stderr (exit 0); a refusal prints its reason to stderr and writes nothing (exit 1). |
| `python -m agent_core invite-list` | Prints every invite as JSON, one per line, newest first, with the received key and the keys-file text; it is the binding source of a keys-file pull request (exit 0, exit 1 when the configuration or the keys file stops the hub). |

Any other argument, and an unknown or repeated option, prints a usage line to stderr and exits 2.

The release query `GET /api/key/release?repository=owner/name&pull_request=N` (KEY-PROTOCOL.md section 8) lets a `service` key with the scope `release:query` ask whether a pull request in one of its `subjects` may leave draft; the answer is signed. The hub computes it from the team record (RECORD-PROTOCOL.md section 9): a pull request that no task links gets `released: false` with `task_missing`, and a hub process that found the record broken answers the signed `503` `record_unverified`. The answer is one input for a guard and replaces no review rule.

The team record (third build step: signed events in PostgreSQL, tasks and security issues) is specified in [RECORD-PROTOCOL.md](RECORD-PROTOCOL.md) and built: the record library, the schema and `python -m agent_core.record migrate`, the commands `export`, `verify` and `restore`, and the record in the hub. The hub verifies the record at start, every 10 minutes and on request, makes a checkpoint attempt at every full hour, which signs the record hash only when the record is not empty, its head moved since the newest checkpoint of the hub key, and no other hub process already wrote the checkpoint of that hour, and serves the signed endpoints under `/api/key/record/` (RECORD-PROTOCOL.md section 7) to keys with the scopes `record:read`, `record:write`, `record:security` and `record:watch`.

### Control

Anyone who can see a session may watch its live terminal on the website. Start, stop, type, and resize are allowed only when the session's origin device row exists, is not revoked, and its `github_login` either equals the signed-in actor or is an agent login that actor owns; the origin device must also have a control-ready WebSocket. The hub relays control and terminal bytes; it does not run processes and does not author session rows. Details are in [PROTOCOL.md](PROTOCOL.md).

### Agent operations

The dashboard shows an **Agents** section to people who own at least one configured agent. It links agent sessions to their chronological timelines, shows current coordinator state, creates assignments, displays issue questions, and posts owner replies. Assignment creation responses and stored activity use GitHub's canonical repository case. Usage is available in lane-token, OpenRouter-account, and Grok tables.

Session timelines keep the newest 5,000 related rows when capped and return those rows in chronological order (oldest first) with `truncated: true`.
The static session page is served on direct entry without a sign-in cookie. If its timeline fetch returns 401, the page shows **Signed out — sign in on the dashboard** and links back to `/`; the page does not poll or expose session data.

Website API endpoints are documented in [PROTOCOL.md](PROTOCOL.md):

- `POST /api/assignments` and `GET /api/assignments?agent=<login>`
- `POST /api/assignments/{owner}/{name}/{number}/comments`
- `GET /api/usage`
- `GET /session?id=<id>`, `GET /api/session?id=<id>`, `GET /api/session/terminal?id=<id>`, `POST /api/session/control?id=<id>`, and `GET /api/session-timeline?id=<id>`

The hub uses only the signed-in human's stored OAuth token for GitHub issue and comment writes. Tokens never enter API responses, activity rows, or browser cookies. Devices remain the sole write owners of their rows; website actions use synthetic `web:<login>` activity origins.

Every assignment creation must include `deviation: {"declared": false}`. This is the assigning owner's explicit statement that no deviation from the target repository's contribution rules is authorized; any deviation instead needs an explicit grant on the issue. The dashboard requires the owner to actively check this statement for each assignment.

Assignment and reply writes accept an optional lowercase UUID `request_id`. User bodies cannot contain the reserved `<!-- agent-core-request:` marker line. The dashboard keeps the same UUID and exact request payload in `sessionStorage` across reloads and sign-in renewal after a timeout or network error, and the hub appends `<!-- agent-core-request:<request_id> -->` to the GitHub issue or comment. The hub binds the id to a fingerprint of the normalized request and reserves each actor, activity type, and request id while the write is in progress; a concurrent request returns 409, as does reuse with different content. A later retry searches back to the reservation's first-reserve time and reuses only an actor-authored marker match with the expected marker-free body, preventing duplicate issues and comments after an ambiguous timeout. Recording the activity and completing its reservation are one transaction, so a superseded lease owner cannot publish a second event.

### Tests

Node.js is required for the dashboard and session page tests.

PostgreSQL 16 (or later) is required for the team record tests. They read the connection string of
a superuser on a throwaway local test server from `AGENT_CORE_TEST_POSTGRES`; each test creates its
own database and roles with random names and drops them at the end, except the identifier test, which
uses the fixed names `grant`, `select` and `order` and removes roles and a database of these names
first. Without the variable every record
test that uses the database fails; none is skipped. The tests of the event text and of the connection
string need no database. The server must accept password logins over TCP on a loopback address or
`localhost`.

Use a throwaway PostgreSQL 16 instance for this and nothing else: it holds no data, the tests
create and drop databases and roles on it, and its superuser password `postgres` is a test
constant. It listens on the dedicated port 55432, so it never meets another PostgreSQL on 5432.
Start it in one of two ways.

With a container:

```bash
docker run -d --rm --name agent-core-test-postgres -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:55432:5432 postgres:16
# Stop and remove it afterwards: docker stop agent-core-test-postgres
```

Or with a local `initdb` of PostgreSQL 16 (its `bin` directory on `PATH`):

```bash
TESTPG="$(mktemp -d)"
printf 'postgres\n' > "$TESTPG/password"
initdb -D "$TESTPG/data" -U postgres --auth=scram-sha-256 --pwfile="$TESTPG/password"
pg_ctl -D "$TESTPG/data" -l "$TESTPG/server.log" -o "-p 55432 -c listen_addresses=127.0.0.1 -k $TESTPG" start
# Stop and remove it afterwards: pg_ctl -D "$TESTPG/data" stop && rm -rf "$TESTPG"
```

Then run the tests:

```bash
export AGENT_CORE_TEST_POSTGRES=postgresql://postgres:postgres@127.0.0.1:55432/postgres
pip install -e ".[test]"
pytest
```

CI runs the same tests against a `postgres:16` service container on the same port 55432.

`python -m agent_core.record migrate` applies the schema of the team record (RECORD-PROTOCOL.md
section 11.4). It reads `AGENT_CORE_RECORD_OWNER_DATABASE` and `AGENT_CORE_RECORD_DATABASE`, and it
runs to completion before every start of the hub; the hub itself holds only
`AGENT_CORE_RECORD_DATABASE`.

`docker-compose.yaml` runs the three pieces of RECORD-PROTOCOL.md section 15: a `postgres:16`
service with TLS that creates the owner role `record_owner`, the writer role `record_writer` and the
database `record` from two secrets, a one-shot `migrate` service, and the hub, which starts only
after `migrate` completed. It needs `AGENT_CORE_RECORD_OWNER_PASSWORD`,
`AGENT_CORE_RECORD_WRITER_PASSWORD` (each 64 lowercase hex characters) and
`AGENT_CORE_RECORD_ADMIN_PASSWORD` (the superuser of that server) in its environment.

Three further commands export, check and restore the record (RECORD-PROTOCOL.md section 12.3). None
of them needs the hub to run, none reads `AGENT_CORE_RECORD_OWNER_DATABASE`, and each prints exactly
one line: no value of the configuration and no message of a database error.

| Command | Reads | Prints |
|---|---|---|
| `python -m agent_core.record export --out FILE` | `AGENT_CORE_RECORD_DATABASE` (writer role) | `exported <position> <record hash or ->` (exit 0); `refused config`, `refused file_exists`, `refused file_write`, `refused database` or `refused value_unwritable` (exit 1) |
| `python -m agent_core.record verify --keys KEYS --teams TEAMS --in FILE [--hub-key ID=PUBLIC_KEY]...` | the files named; no variable, no database | `verified <position> <record hash or ->` (exit 0); `refused config` or `failed <position> <failure>` (exit 1) |
| `python -m agent_core.record restore --in FILE [--hub-key ID=PUBLIC_KEY]...` | `AGENT_CORE_RECORD_DATABASE` (writer role), `AGENT_CORE_KEYS`, `AGENT_CORE_TEAMS` | `restored <position> <record hash or ->` (exit 0); `refused config`, `failed <position> <failure>`, `refused hub_key_missing`, `refused schema_missing (run python -m agent_core.record migrate first)`, `refused schema_version`, `refused writer_rights`, `refused not_empty` or `refused database` (exit 1) |

`export` creates `FILE` with mode `0600`, refuses an existing `FILE`, and removes `FILE` again when it
fails after creating it; an export counts only when it printed `exported`. The file contains security
issues: keep it encrypted and out of every repository, issue, pull request and log (section 12.4).
`verify` checks the file offline against the public keys file and teams file, and checks every
checkpoint whose hub key id is given with `--hub-key`. `restore` verifies the whole file first, then
inserts it into a migrated, empty database; run `migrate` before it, and give the public key of every
hub key id in the file. Without a command, or with an unknown one, the program prints
`usage: python -m agent_core.record {migrate,export,verify,restore}` to stderr and exits 2.

### License

MIT. See [LICENSE](LICENSE).
