# DBG-01 – contract changes

ADR-027 section 3 (additive). Decision: ADR-102 (sessions query the DFX debug endpoint through the platform).

## Credential body (`credentials/dfx_debug.json`)
New kind `dfx_debug` (`common.json` `credential_kind`), one record per user: `address`, `signature`, `api_url`
(`https` only) and the optional release `use {wgs}` (the workgroups whose sessions of the owner's tasks may query).
`api/me-credentials-rotate.request.json`: `records` holds up to 5 records (one per single kind).

## Operation `dfx.debug` (`ops/`)
New type `dfx.debug` (class `read`, no dedupe): parameters `table`, `select`, `where`, `groupBy`, `orderBy`, `limit`,
`offset` (the DTO of the DFX API's `POST /support/debug`); result `{table, keys, rows, rows_total, truncated}`; new
`$defs` `dfx_debug_identifier`, `dfx_debug_scalar`, `dfx_debug_select_item`, `dfx_debug_where`, `dfx_debug_cell`.
`events/objects.json` `body_operation_params` gains its alternative.

## MCP tool `dfx_debug` (`mcp/`)
New platform tool `dfx_debug`: arguments the DTO of `dfx.debug`, answer the operation's outcome (as
`request_operation`), journaled as `operation.request`; `ops/classes.json` `tools` maps it to `dfx.debug`.
`guest/journal/record.json` names it among the tools of `platform.refused`.

## Audit
Outbox action `dfx.read` (detail `{operation_id, owner_id, table, filter, rows, result}`; `filter` the WHERE tree's
columns and operators, never a value).

## Guest
Version 0.8.0: the contracts the guest bundles change (the tool `dfx_debug`, the operation result).
