# ATT-01 – contract changes

ADR-027 section 3 (additive). Decision: ADR-102 (files and images attached to prompts).

## Event body `prompt` (`events/objects.json`)
- `attachments` was reserved and had to be empty; it now holds up to 10 `attachment_ref`
  `{object, name, type, bytes, epoch, salt, digest}` (new `$defs/attachment_ref`, 04-crypto.md section 4.14). An empty
  list stays valid, so every prompt written before is still valid.

## API (`api/INDEX.json`)
| Endpoint | Purpose |
|---|---|
| `PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}` | upload one sealed chunk of an attachment (`task:write`) |
| `POST /v1/workgroups/{wg}/attachments/{object}/commit` | seal the upload with `{chunks, size, digest}` |
| `GET /v1/workgroups/{wg}/attachments/{object}/chunks/{index}` | one sealed chunk of a complete attachment (`wg:read`) |
| `GET /v1/device/attachments/{object}/chunks/{index}` | the same for a device assigned to the workgroup |

## Audit
Actions `attachment.stored` (detail `{chunks, size}`), `read.workgroup.attachment.chunk` and
`read.device.attachment.chunk`.

## Vectors
New `vectors/attachment.json`.
