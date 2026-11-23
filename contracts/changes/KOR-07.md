# KOR-07 – contract changes

## Passkey reference `passkey_id` in the `auth.*` audit details (defect fix)

`audit/auth.login.json`, `auth.passkey_add.json`, `auth.passkey_remove.json` and `auth.register.json` required a
property `credential_id`. The hub refuses every detail key that names confidential material (`credential`, `token`,
`secret`, ...; `agent_hub.audit.chain`), so it could never write the contract's field and wrote an empty detail
instead. The property is renamed to `passkey_id` and its pattern is the passkey's audit reference that the hub already
uses as the row's `target_id`: the b64u credential ID, or `sha256:<32 hex>` of it when the b64u text is longer than
128 characters (`^(?:[A-Za-z0-9_-]{1,128}|sha256:[0-9a-f]{32})$`). Fixtures renamed accordingly.

## `state` in the `device.enroll` audit detail (additive)

`audit/device.enroll.json` gets the optional property `state` (`active` for an org device, `pending_certification`
for a platform device): the hub has always recorded it, and the state after the enrollment is the only metadata that
tells an org enrollment from a platform enrollment awaiting certification. Fixtures `valid/with-state.json`,
`invalid/unknown-state.json`.

## Runner pings audited at the hub (no contract change)

`ping.delivered` of the runner's own pings (`subagent.finished`, `operation.finished`) is accepted without a
`session_ping` row (05-runner.md section 9); the detail contract is unchanged.

## `credential` source of `runner.inbound_rejected` (additive)

`audit/runner.inbound_rejected.json` and the same detail in `outbox/objects.json`: the closed `source` list gets
`credential`, so the runner can audit a credential record it refuses (signature, author, header, rollback; decisions
RUN-09 items 10 and 24). Fixture `valid/credential.json`.

## Journal kinds `platform.read` and `platform.refused` (additive)

03-api.md section 8: "every call is journaled; every read is also an audit row". Two kinds are added to
`guest/journal/` (`INDEX.json`, `record.json`, one body file each, fixtures):

- `platform.read {tool:"get_notifications", result_count}` - the one read the guest answers itself (its pings). The
  runner records the `platform.read` event (adding `arguments: {}`, the event body of 03-api.md section 10) and the
  outbox audit `read.session.get_notifications` exactly as for a forwarded read; before, this read was neither
  audited nor recorded.
- `platform.refused {code, tool}` - an MCP request of the session refused before it was forwarded or answered:
  `parse_error`, `invalid_request`, `method_not_found`, `unknown_tool`, `params_invalid`; `tool` is the name when it
  is one of the twelve tools of `mcp/INDEX.json` (a test keeps the lists equal), else null; only `params_invalid`
  may name a tool (two `oneOf` variants). Metadata only: never the method text, a foreign tool name or arguments. A
  refused read tool is audited by the runner as `read.session.<tool>` with result `rejected` and `result_count` 0.
