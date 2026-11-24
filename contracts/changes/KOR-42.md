# KOR-42 – pairing an agent that runs on another computer (ADR-058, 11-agent-access.md section 5.4)

Additive contract changes; an agent, page or hub of before KOR-42 keeps working with a loopback pairing.

- `api/agent-pairings-create.request.json`: optional `remote` (boolean). `true` creates a pairing from another
  computer; absent or `false` is the loopback pairing as before. A hub of before KOR-42 refuses the field
  (`400 request_invalid`); the agent tool then reports `hub_too_old`.
- `api/agent-pairings-create.response.json`, `api/agent-pairings-get.response.json`: `remote` (`true`) and `nonce`
  (b64u, 16 bytes; input of the agent code), both present only for a remote pairing. A loopback pairing is answered
  exactly as before.
- `api/me-agents-confirm.request.json`: optional `handover_ct` (the handover sealed to the agent); required for a
  remote pairing, refused for a loopback pairing.
- `api/me-agents-list.response.json`: `remote` (boolean) on every row.
- `api/INDEX.json`: `GET /v1/agent/handover` (`agent-handover`, section `11.4`, auth `agent`, right
  `agent:confirmed`, read resource `agent.handover`) → `{agent_id, user_id, handover_ct}`
  (`tests/platform/contracts/spec/endpoints-kor42.json`).
- `enums/error-codes.json`: `handover_required` (400), `handover_unexpected` (400).
- `enums/audit-actions.json` and `audit/`: `read.agent.handover`; `audit/agent.confirm.json` and
  `audit/agent.claim.json` gain the optional detail `remote` (`true`, present only for a remote pairing).
- `api/objects.json` `notification_row.ref` of `agent.account_change`: the new action `agent_paired_remote`
  (`agent_paired_remote::<new agent ID>`, empty agent field: written for the browser session that confirmed). The
  pattern already admits it.
- `vectors/agent.json`: section `remote` – agent code (input, code, shown form), handover sealed to the agent (info,
  plaintext, sealed), return code (info, code, shown form), typed forms of a code; negative vectors of the ops
  `agent_code`, `parse_code`, `open_handover`.
