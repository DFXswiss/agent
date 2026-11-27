# KOR-73 – contract changes

ADR-027 section 3 (additive). Decision: ADR-100 (parallel subscription logins, each in its own sandbox, own mailbox
per subscription, registration).

## Credential body (`credentials/claude_subscription.json`)
- New optional `mailbox` (the body of `credentials/mailbox.json`): the subscription's own mailbox, written by the
  owner's client, kept by every login and renewal.

## Device requests (`requests/claude.login.*`)
- Root: new optional `account {name, birth_date?, consents?}` (registration; consents `terms`, `age`,
  `marketing`, only what the owner gave explicitly).
- Follow-ups: new `{click: {x, y}}` (fractions of the picture) and `{relay: true}`.
- Responses: new step `verify {frame, image}` (a check for humans, the sandbox's picture); `authorize` gains the
  optional `reason`; new progress stages `mail_code_received`, `registered`, `verified`.

## API
- `POST /v1/device-requests`: new optional plaintext `slot` (UUID). An active root is replaced only by a new root of
  the same user, device, kind and slot (03-api.md section 4.10).
