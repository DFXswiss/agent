# KOR-28 – contract changes

ADR-027 section 3 (description texts of a defect fix; no schema change). ADR-012 amendment KOR-28.

## `ops/dedupe-fields.json`: the idempotency key no longer replaces the dedupe fields

With an idempotency key the runner used `{idempotency_key, task}` instead of the type's fields, so a session that
passed a new key repeated the same operation (the same pull-request comment twice). The content fields of a type now
apply with or without a key; the key only marks a repetition as the same request, and a key reused for other content
of the same task and type is rejected by the runner with `params_invalid` (`/idempotency_key`). The top-level
`description` says so; `idempotency_key.window_rule` now names only the case that remains (a type without dedupe
fields, 86400 s). Field sets, windows and tags of every type are unchanged.

## `mcp/objects.json` `idempotency_key`, `vectors/README.md`

The description of the MCP argument and the vector table say the same. The vector `dedupe.json` is unchanged: its
keyed tag is still the tag of the key's field set, which the pipeline now uses only for types without dedupe fields.
