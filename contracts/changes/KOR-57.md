# KOR-57 – the main orchestrator's own identity (ADR-082)

Additive contract changes:

- `common.json`: membership-log body `log_body_agent_add` (in `log_entry_body`), the optional member `agent`
  (`log_agent_member` `{id, pub, gen, cert}`) of `log_body_genesis` and `log_body_accept`, `agent_gen`; hub error
  code `log_agent`.
- `log/membership/agent_add.json`.
- `enums/error-codes.json`: `log_agent` (400).
- `vectors/agent.json`: `own_identity` (delegation certificate, the user's anchor sealed to the agent) and the
  negatives `cert_*`, `user_anchor_*`.
- `vectors/log.json`, `log-rotation.json`, `log-wraps.json`, `log-events.json`: the agents log (`logs.agents`),
  `agents_valid`, the `agent_*`/`accept_*`/`genesis_*` negatives (`log_agent`), rotation cases with `losers`, `lost`,
  `self_owed`, the agent batches, the `agents` sections of the wrap and event files, the operations `cred_readers`
  and `revoked_agents` and the optional `logs` of `check_cred_wrap`; the identities list the agents `bot`…`bot6`.

Stage b – the main orchestrator as a platform session (05-runner.md section 17, additive):

- `guest/orchestrator-keys.{request,response}.json`, `guest/orchestrator-handover.{request,response}.json`,
  `guest/orchestrator-end.{request,response}.json`, `guest/relay-answer.{request,response}.json` with their rows in `guest/INDEX.json` (03-api.md section 7.1).
- `guest/objects.json`: `relay_request_headers`, `relay_response_headers`.
- Journal kind `hub.relay` (`guest/journal/hub.relay.json`, `record.json#/$defs/body_hub_relay`, `INDEX.json`).

Stage b – the main orchestrator on platform hardware (ADR-082 sections 6 and 9.6-9.9, 11-agent-access.md section 11),
additive:

- `api/INDEX.json` (section `11.4`, owner KOR-57): `POST` and `GET /v1/sessions/{session}/orchestrator` (`owner`,
  `session-orchestrator-request`, `session-orchestrator-get`, read resource `session.orchestrator`),
  `POST /v1/device/orchestrator-pairings` and `GET /v1/device/sessions/{session}/orchestrator` (`device:session`,
  read resource `device.orchestrator`), `POST /v1/agent-pairings/{pairing_id}/orchestrator-handover` (`owner`), with
  their request and response schemas (`tests/platform/contracts/spec/endpoints-kor57.json`).
- `api/device-state.response.json`: optional `orchestrators: [{session_id, step: "keys" | "handover" | "revoked",
  pairing_id}]` (`revoked`: the hub revoked the newest pairing of a running session; the guest ends it).
- `ws/device`: the hint `session.orchestrator {session_id}` (`objects.json`, `session.orchestrator.json`,
  `ws/INDEX.json`).
- `enums/error-codes.json` and `common.json`: `orchestrator_unavailable` (409) with `detail.reason` ∈ `not_main`,
  `not_private`, `not_platform`, `not_running`, `not_requested`.
- `enums/audit-actions.json` and `audit/`: `session.orchestrator`, `agent.handover` (`{agent}`),
  `read.session.orchestrator`, `read.device.orchestrator`.
- `vectors/agent.json`: section `orchestrator` (the device's signature over `P‖"orchestrator-key\0"‖canonical({"fp",
  "pairing_id","session"})`, the owner's over `P‖"orchestrator-handover\0"‖canonical({"ct","pairing","session"})`) and
  the negatives of the ops `verify_orchestrator_key` and `verify_orchestrator_handover`.
