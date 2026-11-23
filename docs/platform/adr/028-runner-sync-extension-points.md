# ADR-028: Runner sync extension points (connect hooks, heartbeat sections)

Status: Accepted (2026-11-21)

## Context
03-api.md §6.2 and ADR-013 fix the order after every WebSocket (re)connect of the runner: reconcile, then push the
outbox, then pull all cursors; the heartbeat every 15 s carries capacity, sessions and VMs. The sync area (RUN-02) owns
the socket and the push, but reconcile and pull belong to RUN-03 and the heartbeat contents to VM-02 and RUN-04.
ADR-027 §4 lists no extension point for either, and a package must not edit another package's module or a shared list.
The existing `reconcile.py:STEPS` point describes the steps *inside* the reconcile engine, not the trigger.

## Decision
The sync area discovers two further points with `registry.discover` (areas in sorted order), implemented in
`agent_runner/sync/extensions.py`:

| Module and attribute | Value |
|---|---|
| `<area>/connect.py:HOOKS` | sequence of `ConnectHook(phase, name, run)`; phases `reconcile` and `pull`. After every (re)connect the `reconcile` hooks run, then `Pusher.drain()`, then the `pull` hooks, then hints are dispatched. Names are unique; a failing hook is logged by exception type and does not end the connection. |
| `<area>/heartbeat.py:SECTIONS` | sequence of `HeartbeatSection(field, provide)`; `field` is `capacity`, `sessions` or `vms`, at most one provider per field. A field without a provider is sent empty. |

RUN-03 registers one `reconcile` connect hook that triggers its engine (which runs the `reconcile.py:STEPS`), and one
`pull` hook for its pull loop; it does not register the steps twice.

## Consequences
- The reconnect order is enforced in one place (`ws.py`) and tested there with a probe package.
- The device is online (heartbeats) before VM-02 and RUN-04 provide real contents.

## Alternatives considered
Callbacks passed into the WebSocket client by the daemon (requires editing RUN-01's lifecycle), hint handlers for a
synthetic `connected` hint (no ordering guarantee between areas), reconcile steps for the trigger (mixes engine steps
and connection events).

## Deviation from DESIGN.md
None beyond ADR-001.
