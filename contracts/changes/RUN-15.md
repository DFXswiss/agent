# RUN-15 – contract changes

ADR-027 section 3 (additive). Producer: the guest `GET /v1/metrics` (`agent_guest.metrics`); consumer: the runner's
metric sampler (`agent_runner.metrics`, 08-monitoring.md sections 1-3).

## `guest/objects.json#/$defs/session_metrics`: optional session counters (additive)

08-monitoring.md section 3 exports the per-session counters `agent_runner_session_oom_kills_total` (cgroup
`memory.events oom_kill`, section 1), `agent_runner_session_hook_denies_total`, `agent_runner_session_hook_errors_total`,
`agent_runner_session_egress_denied_total` and `agent_runner_session_model_tokens_total{direction}`, and section 1 takes
the session network value from the gateway's request/response bytes. 03-api.md section 7.1 had none of these fields,
so the runner could not export them. The guest is the one place that sees all of them: the cgroup, the journal records
of the hooks, the gateway's `usage` records and the egress filter of the session user.

New optional `count` properties (absent = not measured; the guest of RUN-15 always sends them):

| Property | Meaning |
|---|---|
| `oom_kills` | `memory.events` `oom_kill` of the session's unit |
| `hook_denies` | `session.hook` records with decision `deny` or `block` |
| `hook_errors` | `guest.error` records with code `hook_handler_failed` |
| `egress_denied` | packets of the session user that the egress filter refuses (05-runner.md section 4.3) |
| `model_input_tokens` / `model_output_tokens` | `usage`: input + cache read + cache write / output + thinking tokens |
| `model_request_bytes` / `model_response_bytes` | `usage`: `request_bytes` / `response_bytes` |

The guest counts the journal records of each active session; after a restart of the guest control process it counts
again over the records the journal still holds, so a value can drop when records were pruned meanwhile (a counter
reset for Prometheus). `egress_denied` belongs to the session, never to a reused UID. The journal body `metrics` refers to the same definition and accepts the fields as well.

Fixtures: `valid/session-counters.json`, `invalid/negative-counter.json`, `invalid/string-counter.json` under
`fixtures/guest/metrics.response/`. `tests/platform/contracts/test_ctr02.py` lists the new nested fields.
