# KOR-22 – contract changes

ADR-027 section 3 (additive). A registration whose new passkey gives no PRF output cannot unlock the user's keys; the
browser withdraws it at once instead of leaving it to the 24 h orphan job (ADR-055, `03-api.md` §4.1).

## `api/me-registration-delete.{request,response}.json`, `api/INDEX.json` (additive)

`DELETE /v1/me/registration` (browser, `user`) → `{}`. Deletes everything the signed-in user stored and frees the
invitation (`registered_user` reset; the invitation is usable again until it expires), only while the registration is
unfinished: the invitation is unused, the user is no org member, holds no platform role and owns no task; otherwise
`403 forbidden`. Audited `invite.release` with actor `user` (same detail as the orphan job). Old clients never call
it; a new client against an old hub gets `404` and tells the user that the invitation is free again after 24 h.
