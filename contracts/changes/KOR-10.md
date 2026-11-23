# KOR-10 – contract changes

All additive (AUD-02 NB-03, NB-07, NB-08); 03-api.md sections 8, 9 and 11 name them.

## Operation error code `no_changes`

`enums/operation-error-codes.json` and `common.json#/$defs/operation_error_code`: a `github.commit` whose workspace
snapshot has no change against the parent (`files_changed` 0) is rejected at the check with `no_changes` (not
retryable; the hub registers it as locally rejected, so its lists of operation error codes must know it). Before, the runner sent an empty tree to GitHub, which answered `422` and the operation ended as
`upstream_error`. The hub's copy of the list (`agent_hub.outbox.bodies`), the generated audit detail schemas and the
error catalog of the web (texts in `web/scripts/error-texts.json`) follow. Fixture `ops/error/valid/no-changes.json`.

## MCP tool error `rate_limited`

`mcp/objects.json#/$defs/tool_error`: reads of a session are limited by a token bucket in the guest; a read over the
limit is answered with `rate_limited`. Journaled refusals are limited per session by a second bucket (03-api.md
section 8), which replaces "no read is dropped" of KOR-07 (agent decision 3). Fixture `mcp/tool-error/valid/rate-limited.json`.

## Journal `platform.refused`: three more refusals

`guest/journal/record.json#/$defs/body_platform_refused` (B-08 rest, KOR-07 decision 2):

- `size_exceeded` with `tool` null: a request of an authenticated session over the 1 MiB body limit of `/mcp`.
- `not_found` with `tool` `get_subagent_result`: the guest refuses the session's own ID at once.
- `rate_limited` with the name of a read tool (every tool but `request_operation`).

The runner audits a refused read tool as `read.session.<tool>` with result `rejected` (KOR-07). Fixtures in
`guest/journal/platform.refused/` (three valid, three invalid).

## `journal_through` in the budget of a session credential (AUD-02 NA-10)

`guest/session-credential.request.json`, `budget` gets the optional integer `journal_through`. The spent amount of a
renewal is the organization's; the guest gateway carried its own observed cost only as far as the spent amount did not
grow (A-29), so cost of other sessions and devices displaced its own cost the hub had not counted yet. The runner now
adds to `spent_usd` the cost of its `usage` items the hub had not counted in that amount (outbox items above the
`last_origin_seq` of the same `GET /v1/device/state` snapshot) and names in `journal_through` the last journal record
of the guest it had taken into its outbox; the gateway carries the cost of its own requests above it. Fixtures
`valid/platform-budget-journal-through.json`, `invalid/negative-journal-through.json`.
