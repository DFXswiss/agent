# KOR-28 decisions (duplicate protection independent of the idempotency key)

The runner change and its decisions are in `agent` (`docs/platform/decisions/KOR-28.md`). In this repository:

1. Contract changes are description texts only (`contracts/changes/KOR-28.md`): `ops/dedupe-fields.json`,
   `mcp/objects.json` `idempotency_key`, `vectors/README.md`. Field sets, windows, vectors and schemas are unchanged,
   so the hub needs no code change: it compares the one tag of a registration as before.
2. No hub source change: the reference crypto `agent_hub.crypto.dedupe` (CRY-01) and `agent_hub.operations.types`
   stay as they are; `idempotency_fields` is still the key's field set of a type without dedupe fields.
3. E2E-22 (`e2e/platform/specs/e2e-22-formal-checks.spec.ts`, scenario `e2e-22-formal-checks.json`) gets the step
   `rekeyed` (the same comment with a new key: `duplicate`), and the reused key with another body is now `rejected`
   `params_invalid` instead of `duplicate`. The spec could not run in this package (the runner device is production);
   its lint and type checks ran.
4. Shared documentation (spec 00, 01, 03, 04, 07, ADR-012 amendment, deviation register §9) is identical in both
   repositories.
