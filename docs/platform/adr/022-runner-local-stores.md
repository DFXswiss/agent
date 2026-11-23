# ADR-022: Runner and guest local stores

Status: Accepted (2026-11-21)

## Context
KONZEPT §6: all relevant events are stored immediately and locally on the execution device. The `agent` repository uses a local PostgreSQL cluster (`agent_cli.pg`: loopback only, own port, trust auth, identity file outside the DB). Spike B's guest used SQLite. The VM boot path must stay small (ADR-009).

## Decision
- **Runner host**: own PostgreSQL cluster under `$AGENT_RUNNER_HOME/pg` (default `~/.local/share/agent-runner`), managed with the `agent_cli.pg` pattern, schema `runner` (`02-data-model.md` §4). Device identity in `device.json` + `keys/device.key` (0600), outside the database. Scope keys at rest encrypted with a storage key derived from the device seed.
- **Guest**: SQLite journal `/var/lib/agent-guest/journal.db` (owner root control process, 0600), stdlib only; records pruned 24 h after runner acknowledgement.
- Never shares data directories, ports or launchd labels with an existing `agent` installation.

## Consequences
- An event is durable in the guest journal before the runner sees it and durable in the runner outbox before the hub sees it.
- PostgreSQL is a host prerequisite of the runner (as for `agent`).

## Alternatives considered
SQLite on the host (diverges from repository practice, weaker concurrency for the async daemon), PostgreSQL in the guest (boot time, image size).

## Deviation from DESIGN.md
None (DESIGN §8 local PostgreSQL on loopback is followed for the host).
