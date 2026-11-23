# KOR-37 – direct admission of existing accounts (ADR-060)

Additive contract changes:

- `common.json`: membership-log body `log_body_admit` (in `log_entry_body`), platform-log bodies
  `platform_body_user_certify` and `platform_body_user_decertify` (in `platform_entry_body`); error detail `devices`
  (`peer_outdated`); hub error codes below.
- `log/membership/admit.json`, `log/platform/user_certify.json`, `log/platform/user_decertify.json` with fixtures.
- `api/INDEX.json`: `GET /v1/users/lookup` (`user-lookup`), `GET /v1/users/{user}/identity` (`user-identity`),
  `GET /v1/me/invitees` (`me-invitees`), all right `user`; `GET /v1/platform/users/{user}` (`platform-user`, root);
  `api/objects.json` `user_identity`; notification kind `org.admitted` in `notification_row` (`ref` = the admitting
  admin).
- `enums/error-codes.json`: `log_admit` (400), `identity_unproven` (404), `peer_outdated` (409), `account_code_invalid`
  (400).
- `enums/audit-actions.json` and `audit/`: `log.admit`, `platform.user_certify`, `platform.user_decertify`,
  `read.user.identity`, `read.user.lookup`, `read.me.invitees`, `read.platform.user`.
- `vectors/log.json`: log `admit` with `admit_valid` (heads, projections) and the `admit_*` negatives; the platform log
  of the vectors has 17 entries (user anchors); member projections carry `consented`. `vectors/platform-log.json`:
  `users` projection and the user-certification negatives. `vectors/identity.json` (new): identity proof, invited
  user ID, account code and its parser.
- Live channel `GET /v1/device/ws`: the runner announces capabilities in the header `Agent-Features` (no query
  parameter, so an older hub keeps accepting the upgrade); `log.admit` is the first.
