# HUB-04 – contract changes

## `GET /v1/platform/enrollments` returns the platform log (defect fix)

The operator CLI (`agent-hub platform …`, ADR-023) talks to the hub only with root-signed requests (`03-api.md`
section 1). To append it needs the current head (`seq`, hash) of the platform log, and to certify safely it needs the
`device_invite` entries (their `pk_inv`) so that it can check `inv_sig` itself instead of trusting the hub. The only
read of the platform log, `GET /v1/platform/log`, requires a browser session, so no root-signed request could read it.

- `api/platform-enrollments.response.json`: new required property `log` (array of `common.json#/$defs/platform_entry`,
  the whole platform log in `seq` order, empty before `platform init`). The CLI verifies it with P1-P4 against its own
  root key before it builds entries. Fixtures: `valid/basic.json` carries the first two entries of
  `vectors/platform-log.json`, new `invalid/missing-log.json`.

Nothing else changes; no other implementation reads this endpoint.
