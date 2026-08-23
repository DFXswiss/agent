# agent

Local session-store client. Record sessions, activities and (when a skill is attached) tasks on this machine, then pair the device to the [agent-core](https://github.com/DFXswiss/agent-core) hub with GitHub.

Product decisions (visibility, pairing, sync, restore, what we will not build) are in [DESIGN.md](DESIGN.md).

This device is the write owner of its own rows. The local store is PostgreSQL on `127.0.0.1`. `device.json` next to it is the device identity: wiping only the database must not mint a new device. The hub holds a full copy. `agent sync` pushes own events and pulls own catch-up, session-mail inbox snapshots, and person-ping snapshots. `agent restore` rebuilds a wiped database from the hub.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
agent init
```

`agent init` starts a local PostgreSQL cluster (needs `initdb`/`pg_ctl` on `PATH`, or `AGENT_PG_BIN`). `AGENT_PG_DSN` may point at an existing loopback server instead. Data lives in `$AGENT_HOME` or, if that is unset, `~/.local/share/agent`. Identity is `device.json` next to the cluster.

## Pair and sync

```bash
agent pair --hub https://agent.example
# confirm in the browser after GitHub sign-in
agent sync
agent sync --follow   # stay on the hub WebSocket; a dead socket is a loud error
agent restore   # after a wiped laptop; also works if leftover ledger.sqlite is present
# other commands refuse until you move ledger.sqlite aside
```

`AGENT_HUB` may replace `--hub`. There is no default hub URL.

## Record work

```bash
agent session register --id <session-id> --kind human
agent skills path
agent session skill attach --id <session-id> --skill spine
agent session skill attach --id <session-id> --skill review-loop
agent session skill attach --id <session-id> --skill pr-review
agent session start --id <session-id> [--provider grok] [--model TEXT] [--cmd TEXT] [--cols N] [--rows N]
agent session input --id <session-id> --data TEXT
agent session input --id <session-id> --key enter|ctrl-c|tab
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
agent run --task <uuid> [--dry-run]
agent ping send --to some-login --kind review-request --task <uuid> --note "ready"
agent status
agent dashboard
```

This package **is** the runtime: install it locally and run `agent`. There is no second store binary. The packaged files under `src/agent_cli/skills/` **are** the review contract (`spine`, `review-loop`, `pr-review`). `agent skills path` prints that directory. Operator-specific git or deploy rules stay outside this package.

Kind is `human`, `runner`, or `other`. Skills are opt-in (`spine`, `review-loop`, `pr-review`). Without `spine`, task/checklist/round/work/check/`next`/`close-step`/`run` commands refuse. `allow` uses `spine` when it loads a session or task. Without `review-loop`, implementer and reviewer `agent agent` commands refuse. Without `pr-review`, `agent gate` and pr-reviewer `agent agent` commands refuse. `AGENT_SKILLS_DIR` may override the packaged skill directory only when that directory contains `spine/SKILL.md`, `review-loop/SKILL.md`, and `pr-review/SKILL.md`; otherwise the command fails.

Session mail and `pr.merged` notify channel `agent_inbox`. `issue.assigned` does not: `agent watch assigned` knocks the queue head after writing `MANDATE.md` / `QUEUE.md`. The knock text is only `da ist Post id <uuid>`:

```bash
agent knock --once
agent watch pr-merged   # one scan; needs GitHub CLI (`gh`); run from cron if you need a loop
agent watch pending     # one scan; runs subscription.set and query.request against the hub
agent watch grok-usage  # one scan of SuperGrok weekly credits into usage.snapshot
# agent knock (daemon, no --once) also polls grok-usage every 60s
agent watch assigned [--follow]  # allowlisted assignments; needs `gh` and `$AGENT_HOME/watch.json`
```

`agent watch grok-usage` uses the existing Grok login token from the Grok auth file, does not start a Grok session, and does not knock the TUI. Each `usage.snapshot` includes the account email, provider, and subscription tier. The knock daemon (`agent knock` without `--once`) records those snapshots in the background on the same interval.

`agent watch assigned` reads `$AGENT_HOME/watch.json`:

```json
{ "assigned_repos": ["Owner/repo"], "session_id": "assigned" }
```

Missing or empty `assigned_repos` is an error. `session_id` is optional, defaults to `assigned`, and may contain only `A-Za-z0-9_-`. A session already present under that id must be `kind=runner`. The auto-created runner session attaches `spine`, `review-loop`, and `pr-review` (those skills stay opt-in for every other session). The working directory is `$AGENT_HOME/sessions/<session_id>` unless `AGENT_SESSION_ROOT` is set. The first scan only records the `assigned_watch_since` watermark and the assigned session id, and creates no activities. Changing `session_id` after that pin is an error. Later scans enqueue `issue.assigned` on **that one** runner session, push to the hub, write `MANDATE.md` / `QUEUE.md`, and start Grok only if that session is not already attached. The insert does not notify the knock daemon. There is one terminal; further assignments wait in the knock queue until the session writes `issue.assigned.ack` with `payload.assigned_id` set to that activity id. `MANDATE.md` lists session and activity ids. `QUEUE.md` lists ids and urls. Neither file contains issue bodies. Use `--follow` for a 30s loop, or cron for one-shot runs.

### Session terminal control

This device is the only place that starts, stops, or types into a live terminal. Control mutates the owned session row’s `runtime` fields (`tmux_session`, `control`, optional `cols`/`rows`). Foreign sessions stay read-only.

```bash
agent session start --id <session-id> [--cmd "bash -l"] [--cols 80] [--rows 24]
agent session start --id <session-id> --provider grok
# → started <id> tmux=agent-… grok=<uuid>
# later starts resume that uuid; they do not reuse the store session id
agent session input --id <session-id> --data "ls\n"
agent session input --id <session-id> --key enter
agent session stop --id <session-id>
# → stopped <id>
```

`agent sync --follow` announces `control-ready`, applies hub `control` frames on this device, acks them, and publishes `terminal` captures for owned sessions with `runtime.control=attached`. Terminal bytes are not store events.


## Tests

```bash
pytest
```

Tests start an ephemeral PostgreSQL cluster, or use `AGENT_TEST_PG` when that is set (CI). The integration test talks to `agent-core` when that package is importable (install it next to this checkout).

## License

MIT. See [LICENSE](LICENSE).
