# KOR-01 – contract changes

ADR-027 section 3 (defect fix, AUD-01 A-02; ADR-043). An open invitation could be registered through by anyone who
knew its `invite_id`, which the platform log, the inviter listing and the membership log show to others. Registration
now requires a proof of the code.

## `common.json` `$defs/platform_body_user_invite`: `pk_inv` (required)

The platform-log entry `user_invite` carries the public invitation key `pk_inv` (`b64u_32`), like `device_invite`.
Fixtures: `log/platform/user_invite/valid/basic.json` and the invalid ones carry it; new invalid `missing-pk_inv.json`.

## `api/passkeys-register-finish.request.json`: `inv_sig` (required for a first passkey)

`inv_sig` (`b64u_64`) = Ed25519 of `sk_inv` over `P‖"invite-register\0"‖user_id‖"\0"‖challenge`. Further passkeys
(`add_passkey`, `recovery` ceremonies) carry none of the first-passkey fields, as before. Fixtures updated; new invalid
`missing-inv_sig.json`.

## `api/platform-invites-create.request.json`: `pk_inv` (required)

An inviter sends the public key of the code with the invitation ID. Fixtures updated; new invalid `missing-pk_inv.json`.

## `vectors/`

`invite.json`: section `register` (text, `inv_sig`) and negatives `register_other_challenge`, `register_other_user`,
`register_other_code`, `register_short_challenge`, `register_non_ascii_user`, `register_empty_user` (operation `verify_register`). Every
platform-log vector (`platform-log.json`, `log*.json`) has `pk_inv` in its `user_invite` entry.

## B-06: `api/me-recovery-begin.{request,response}.json`, `api/INDEX.json` (additive)

`POST /v1/me/recovery/begin` (browser, `user`) → `{ceremony_id, challenge, options}` (request options as for a
login). Test data `tests/platform/contracts/spec/endpoints-kor01.json`. Fixtures: valid/invalid of both schemas.

## B-06: `api/me-bundles-put.request.json`: optional `assertion` (additive)

`{ceremony_id, credential_id, authenticator_data, client_data_json, signature}`: a passkey assertion over the challenge
of `POST /v1/me/recovery/begin`; lets a signed-in user replace the recovery code at any time. Fixtures: valid
`recovery-rotate.json`, invalid `assertion-without-signature.json`, `assertion-with-statement.json`.

## B-06: `audit/auth.recovery.json`: step `rotate` (additive)

The replacement of the recovery code is audited. Fixture valid `rotate.json`.
