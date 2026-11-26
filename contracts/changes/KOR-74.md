# KOR-74 – contract changes

ADR-027 section 3 (additive).

## `POST /v1/session/renew` (new)

Rule E-37 ("Angemeldet bleiben", ADR-099): a browser that stays signed in renews its session before it ends, signed
by the still valid session key. New schemas `api/session-renew.request.json` (empty body, as `session/end`) and
`api/session-renew.response.json` (`key_id` of the renewed session and its new `expires_at`; for an agent key, which
does not expire, its key ID and `null`), their fixtures under
`fixtures/api/session-renew.*`, and the row in `api/INDEX.json` (owner KOR-74; the specification row is
`tests/platform/contracts/spec/endpoints-kor74.json`).

## Audit action `auth.session_renew` (new)

Every renewal leaves its own audit row (ADR-063 section 1): `enums/audit-actions.json` (group `auth`) and the detail
schema `audit/auth.session_renew.json` (`key_id`, as `auth.session_end`) with its fixtures. No existing schema changes.
