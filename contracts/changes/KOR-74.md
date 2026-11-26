# KOR-74 – contract changes

ADR-027 section 3 (additive). Decision: ADR-099 (sessions read mailboxes through the platform).

## Credential body (`credentials/mailbox.json`)
- New optional `name` (`[a-z0-9][a-z0-9._-]{0,63}`), `purpose` (1-500 characters) and `use {wgs}` (the workgroups whose
  sessions may read the mailbox). The kind `mailbox` holds several records per user (at most 100); the primary one is
  the mailbox of the automatic login.

## API (`api/INDEX.json`, `api/objects.json`)
| Endpoint | Purpose |
|---|---|
| `GET /v1/me/mailboxes` | the caller's mailboxes with their release mirror |
| `GET/PUT/DELETE /v1/mailboxes/{id}` | one own mailbox record; PUT takes `use {wgs}`, the plaintext mirror of the sealed release |
| `GET /v1/device/mailboxes?wg_id=` | the mailbox records released to a workgroup, for an eligible device |

New objects `mailbox_use`, `mailbox_meta`, `mailbox_record`.

## Operation `mail.find` (`ops/`)
New type `mail.find` (class `read`, no dedupe): parameters `mailbox`, `from`, `subject`, `since`, `until`, `folder`,
`limit`, `body`, `id`, `wait_seconds`; result `{mailbox, messages, waited_seconds, timed_out}`; new `$defs`
`mail_message_id`, `mail_message`. `events/objects.json` `body_operation_params` gains its alternative.

## Device request `mailbox.check` (`requests/`)
Optional `mailbox` (a `credentials/mailbox.json` body): check that mailbox instead of the primary one.

## Audit
Actions `read.me.mailboxes`, `read.mailbox`, `read.device.mailboxes` and the outbox action `mail.read` (detail
`{operation_id, owner_id, folder, count, waited_seconds, timed_out, result}`).

## Guest
Version 0.7.0: the contracts the guest bundles change (`mcp/objects.json` references the operation results).
