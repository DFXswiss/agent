# agent

Local session-store client. Record sessions, tasks, checklists and review pings on this machine, then pair the device to the [agent-core](https://github.com/DFXswiss/agent-core) hub with GitHub.

Product decisions (visibility, pairing, sync, restore, what we will not build) are in [DESIGN.md](DESIGN.md).

This device is the write owner of its own rows. The hub holds a full copy. `agent sync` pushes and pulls every event. `agent restore` rebuilds a wiped database from the hub.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
agent init
```

Data lives in `$AGENT_HOME` or, if that is unset, `~/.local/share/agent`.

## Pair and sync

```bash
agent pair --hub https://agent.example
# confirm in the browser after GitHub sign-in
agent sync
agent sync --follow   # stay on the hub WebSocket; a dead socket is a loud error
agent restore   # after a wiped laptop
```

`AGENT_HUB` may replace `--hub`. There is no default hub URL.

## Record work

```bash
agent session register --id <session-id> --kind human
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
agent ping send --to some-login --kind review-request --task <uuid> --note "ready"
agent status
agent dashboard
```

Kind is `human`, `runner`, or `other`.

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

The integration test talks to `agent-core` when that package is importable (install it next to this checkout).

## License

MIT. See [LICENSE](LICENSE).
