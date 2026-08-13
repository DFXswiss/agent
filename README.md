# agent

Local ledger client. Record sessions, tasks, checklists and review pings on this machine, then pair the device to the [agent-core](https://github.com/DFXswiss/agent-core) hub with GitHub.

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
agent restore   # after a wiped laptop
```

`AGENT_HUB` may replace `--hub`. There is no default hub URL.

## Record work

```bash
agent session register --id <session-id> --kind human
agent task create --session <session-id> --workflow implement --title "…"
agent checklist set --task <uuid> --key spec_written --status ja --source human
agent ping send --to some-login --kind review-request --task <uuid> --note "ready"
agent status
agent dashboard
```

Kind is `human`, `runner`, or `other`.

## Tests

```bash
pytest
```

The integration test talks to `agent-core` when that package is importable (install it next to this checkout).

## License

MIT. See [LICENSE](LICENSE).
