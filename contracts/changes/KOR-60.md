# KOR-60 – contract changes

ADR-027 section 3 (additive). Producer: the guest `GET /v1/metrics` (`agent_guest.metrics`); consumer: the runner's
metric sampler (`agent_runner.metrics`, 08-monitoring.md section 3).

## `guest/objects.json#/$defs/session_metrics`: optional counter `sandbox_denials` (additive)

Decision D16 of the concept review: a blocked attempt that reaches the kernel (the hook let the call through and the
second layer refused it - Landlock, the unit's device policy, the file modes) is counted per session in the
monitoring, not written as an audit row. The kernel of the guest image (Ubuntu 24.04, Linux 6.8) has no Landlock
audit records, so the guest counts the refusals where they surface: the `session.tool` journal records of Claude
Code's own tools (not `mcp__*`: platform tools answer from the runner) with `is_error` whose result names
`EACCES`/`EPERM` or their texts ("Permission denied", "Operation not permitted"). It is a text match: a refusal a
program swallows is not counted, and an error that merely quotes such a text (a file named `EPERM`) is.

| Property | Meaning |
|---|---|
| `sandbox_denials` | `session.tool` records of a tool other than `mcp__*` with `is_error` whose result names a kernel refusal |

Counted like the other journal counters of RUN-15 (per active session, again from the journal after a restart of the
control process). The runner exports it as `agent_runner_session_sandbox_denials_total{vm,session}`.

Fixture: `valid/session-counters.json` under `contracts/fixtures/guest/metrics.response/` carries the field.
