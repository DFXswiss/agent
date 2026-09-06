# agent

Local session-store client. Record sessions, activities and (when a skill is attached) tasks on this machine, then pair the device to the [agent-core](https://github.com/DFXswiss/agent-core) hub with GitHub.

Product decisions (visibility, pairing, sync, restore, what we will not build) are in [DESIGN.md](DESIGN.md). That file also locks the deterministic core: scripts execute, checks measure, gates decide, model text is never a transition, and the hub is not a coding control plane. A draft plus local tests is not a finished pull request; see [CONTRIBUTING.md](CONTRIBUTING.md). The frozen local-CI comment schema for private product repositories is [docs/local-ci-v1.md](docs/local-ci-v1.md) (`agent local-ci verify`). Production-error → draft pull request is the opt-in **error-fix** skill on this device, not the hub.

This device is the write owner of its own rows. The local store is PostgreSQL on `127.0.0.1`. `device.json` next to it is the device identity: wiping only the database must not mint a new device. The hub holds a full copy. `agent sync` pushes own events and pulls own catch-up, session-mail inbox snapshots, and person-ping snapshots. `agent restore` rebuilds a wiped database from the hub.

The [A38 standard](docs/a38.md) defines repository-owned local test requirements and author reports using the existing local-CI format. `agent a38` measures and validates reports; the [dfx pr guard](docs/a38-guard.md) explains repository rules and checks author comments without executing pull-request code.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
agent init
```

`agent init` starts a local PostgreSQL cluster (needs `initdb`/`pg_ctl` on `PATH`, or `AGENT_PG_BIN`). `AGENT_PG_DSN` may point at an existing loopback server instead. Data lives in `$AGENT_HOME` or, if that is unset, `~/.local/share/agent`. Identity is `device.json` next to the cluster. It also installs and starts the user-service device daemon (`agent daemon`), which starts knock, the local dashboard, and cli-bridge immediately; daemon `sync --follow` starts only after `agent pair`, once `device.json` has token and hub URL (init-before-pair remains valid). Only `agent init` creates that cluster. Commands that open the store start it again if it is stopped and fail with `run agent init` when it does not exist; they never run `initdb`. `agent pg status` reports the cluster without starting it, `agent pg stop` stops it (refused while a device daemon installed for this `AGENT_HOME` exists, because that daemon would start it again), and `agent daemon --uninstall` stops it together with the user service. `agent daemon --uninstall` refuses to run under a different `AGENT_HOME` than the one the service was installed for. Both commands stop with an error when a service unit exists but cannot be read or records no `AGENT_HOME`. When `AGENT_PG_DSN` is set, `agent init` writes it into the user service so the daemon uses the same server.

## Pair and sync

```bash
agent pair --hub https://agent.example
# confirm in the browser after GitHub sign-in
agent sync
agent sync --follow   # foreground WebSocket; reconnects with backoff on a dropped connection (cap 30s, logs to stderr; not a silent poll; process stays up)
agent restore   # after a wiped laptop; also works if leftover ledger.sqlite is present
# other commands refuse until you move ledger.sqlite aside
```

`AGENT_HUB` may replace `--hub`. There is no default hub URL. After `agent pair`, once `device.json` has token and hub URL, the user-service daemon starts `sync --follow`; a one-shot `agent sync` after pairing is enough for catch-up.

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
agent session start --id <session-id> [--provider grok] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session input --id <session-id> --data TEXT
agent session input --id <session-id> --key enter|ctrl-c|tab
agent session keep-working --id <session-id> [--once|--follow]
agent session stop --id <session-id>
agent activity add --session <session-id> --type message --payload-file ./mail.json
agent task create --session <session-id> --workflow implement --title "…"
agent round start --task <uuid>
agent agent start --session <session-id> --task <uuid> --role implementer --vendor grok --round 1
agent agent finish --id <implementer-uuid> --verdict done
agent agent start --session <session-id> --task <uuid> --role pr-reviewer-quality --vendor grok
agent agent finish --id <reviewer-uuid> --verdict approved
agent check record --task <uuid> --name lint --command "pytest" --result pass
agent gate record --task <uuid> --stage grok-pr --dimension quality --vendor grok \
  --verdict approved --head <sha> --agent <reviewer-uuid>
agent work add --session <session-id> --key standing --closable-by human
agent work set --session <session-id> --key standing --status done --source human --actor-session <session-id>
agent work list --session <session-id>
agent checklist set --task <uuid> --key spec_written --status ja --source human --evidence "spec.md"
agent allow --action claim-done|pr-ready|pr-create|task-done [--session ID] [--task <uuid>] [--draft true|false] [--json]
agent next --task <uuid>
agent close-step --task <uuid> --key KEY --source script|human|runner --evidence TEXT [--status ja|n_a]
agent run --task <uuid> [--dry-run] [--head SHA] [--cwd PATH] [--spec-file PATH] [--no-tmux]
agent github pending
agent query --match-file PATH
agent subscribe list|set --file PATH|clear
agent mail pending|ingest
agent ping send --to some-login --kind review-request --task <uuid> --note "ready"
agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]
agent status
agent dashboard
agent cli-bridge
agent pg status
agent pg stop
```

`agent run` records a local check when `local_check_pass` is open and the snapshot has no local checks yet (it does not rerun an existing failed check). It closes an agent step when the session store already has the artifact, and with `--spec-file` launches the vendor lane (tmux by default; `--no-tmux` for a subprocess). When `pushed` is open it git-pushes (no force) and closes with the HEAD sha; when `mergeable` is open it measures GitHub mergeability and checks and closes only if both are green. Reviewer lanes are not auto-approved from `STATUS: complete`.

`agent github pending` is one scan: owned pending `pr.open`, `comment.post`, `review.post`, and `issue.write` rows via `gh`. Pull requests are drafts. A retry reuses an existing open draft, issue, or comment instead of creating a second one.

This package **is** the runtime: install it locally and run `agent`. There is no second store binary. The packaged files under `src/agent_cli/skills/` **are** the skill contracts (`spine`, `review-loop`, `pr-review`, `error-fix`). `agent skills path` prints that directory, or `AGENT_SKILLS_DIR` when set. Operator-specific git or deploy rules stay outside this package.

Kind is `human`, `runner`, or `other`. Skills are opt-in (`spine`, `review-loop`, `pr-review`, `error-fix`). Without `spine`, task/checklist/round/work/check/`next`/`close-step`/`run` commands refuse. `allow` uses `spine` when it loads a session or task. Without `review-loop`, implementer and reviewer `agent agent` commands refuse. Without `pr-review`, `agent gate` and pr-reviewer `agent agent` commands refuse. Without `error-fix` the production-error loop does not run. `AGENT_SKILLS_DIR` may override the packaged skill directory only when that directory contains `spine/SKILL.md`, `review-loop/SKILL.md`, and `pr-review/SKILL.md`; otherwise the command fails. `error-fix` ships in the packaged tree; an override need not copy it. Unset `AGENT_SKILLS_DIR` and run `agent skills path` again to print the packaged directory.

Session mail, `pr.merged`, and `error.seen` notify channel `agent_inbox`. `issue.assigned` does not: `agent watch assigned` knocks the queue head after writing `MANDATE.md` / `QUEUE.md`. The knock text is only `da ist Post id <uuid>`:

```bash
agent knock --once
agent knock             # valid foreground loop; the user-service daemon is the supported always-on path
agent watch pr-merged   # one scan; needs GitHub CLI (`gh`); the device daemon covers the loop
agent watch pending     # one scan; runs subscription.set and query.request against the hub
agent watch grok-usage  # one scan of SuperGrok weekly credits into usage.snapshot
agent watch assigned [--follow]  # allowlisted assignments; needs `gh` and `$AGENT_HOME/watch.json`
agent watch errors      # one scan; $AGENT_HOME/error-fix.json; no log host in this package
agent watch error-fix   # one scan; find-or-create implement task + isolated worktree
agent supervise --session ID [--repo OWNER/REPO --number N] [--once|--follow]
# agent knock (daemon, no --once) polls grok-usage, pending, pr.merged, github pending, mail pending, errors, and error-fix every 60s
```

`agent supervise` posts a short status line to Telegram when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the environment. The follow CLI does not ask closed questions. Working vs not working for paging is whether the Grok tmux session exists: it posts `not working` only when that session is gone, not when the prompt is idle between turns. The TUI working probe (`Thinking…`, `Waiting for response`, `Preparing …`, `[stop]`, `Esc:cancel`, `command still running`, queued `Enter to send now`) is for the follow loop, not for Telegram. A send failure is printed to stderr and does not stop the loop. Credentials stay out of git.

The error-fix executor find-or-creates the implement task and isolated worktree; `agent github pending` still opens draft pull requests.

`agent watch grok-usage` uses the existing Grok login token from the Grok auth file, does not start a Grok session, and does not knock the TUI. Each `usage.snapshot` includes the account email, provider, and subscription tier. Under the device daemon, the knock child records those snapshots (and scans pending, `pr.merged`, github pending, mail pending, errors when `$AGENT_HOME/error-fix.json` exists, and pending `error.fix`) on the same interval. `agent daemon --install` / `--uninstall` manage the user service; `agent init` already installs and starts it.

`agent watch assigned` reads `$AGENT_HOME/watch.json`:

```json
{ "assigned_repos": ["Owner/repo"], "session_id": "assigned" }
```

Missing or empty `assigned_repos` is an error. `session_id` is optional, defaults to `assigned`, and may contain only `A-Za-z0-9_-`. A session already present under that id must be `kind=runner`. The auto-created runner session attaches `spine`, `review-loop`, and `pr-review` (those skills stay opt-in for every other session). The working directory is `$AGENT_HOME/sessions/<session_id>` unless `AGENT_SESSION_ROOT` is set. The first successful scan records the `assigned_watch_since` watermark and the assigned session id, and creates no activities. Changing `session_id` after that pin is an error. The scan uses the paired GitHub login; a missing pair or a `gh api user` mismatch is an error. Later scans enqueue `issue.assigned` on **that one** runner session, push to the hub, write `MANDATE.md` / `QUEUE.md`, and start Grok only if that session is not already attached. The insert does not notify the knock daemon. There is one terminal; further assignments wait in the knock queue until the supervise script records `issue.assigned.ack` with `payload.assigned_id` set to that activity id. The follow CLI does not ack from pane text. `MANDATE.md` lists session and activity ids. `QUEUE.md` lists ids and urls. Neither file contains issue bodies. Use `--follow` for a 30s loop, or cron for one-shot runs.

### Session terminal control

This device is the only place that starts, stops, or types into a live terminal. Control mutates the owned session row’s `runtime` fields (`tmux_session`, `control`, optional `cols`/`rows`). Foreign sessions stay read-only.

```bash
agent session start --id <session-id> [--cmd "bash -l"] [--cols 80] [--rows 24]
agent session start --id <session-id> --provider grok
# → started <id> tmux=agent-… grok=<uuid>
# later starts resume that uuid; they do not reuse the store session id
agent session input --id <session-id> --data "ls\n"
agent session input --id <session-id> --key enter
agent session keep-working --id <session-id> --once
agent session stop --id <session-id>
# → stopped <id>
```

`agent session keep-working` is for a session whose standing job is already in the working directory. The Grok TUI stops after each turn; this command does not interrupt an in-flight turn. A tool-approval modal is cleared with Enter. The first idle composer tick (caret visible) sends one standing instruction to keep going until the assignment is complete. Later idle ticks send only `Continue.`. Bare `keep-working` and `--once` are one tick; `--follow` polls every 30s. A missing tmux session is reported and left alone. An unreadable pane is not typed into.

`$AGENT_HOME/runtime-targets.json` may map a session id to an argv list prepended to every `tmux` call for that session. A missing key is local tmux, as today. The file stays on this device; it is not a hub event.

`agent cli-bridge` (started by the device daemon) accepts allowlisted store/spine argv on loopback. It does not run `session start`, `run`, GitHub, or mail. `python -m agent_cli.stub` is the matching client. Set `AGENT_CLI_BRIDGE=127.0.0.1:7846` on the client. The server bind is `127.0.0.1` or `::1` (`AGENT_CLI_BRIDGE_BIND`).

`agent sync --follow` announces `control-ready`, applies hub `control` frames on this device, acks them, and publishes `terminal` captures for owned sessions with `runtime.control=attached`. Terminal bytes are not store events.

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
