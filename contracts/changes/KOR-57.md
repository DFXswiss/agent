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
