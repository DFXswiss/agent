# ADR-102: Files and images attached to prompts, sealed as objects of their own

Status: Accepted (2026-11-27)

Implements [E-43](../KONZEPT.md#e-43) and extends [§6 Punkt 2](../KONZEPT.md#k-6-b2) (prompts are encrypted content);
keeps the zero-knowledge hub of [ADR-003](003-zero-knowledge-hub.md) and follows the sealed objects of
[ADR-072](072-session-context-resume.md) and [ADR-080](080-test-runs-as-operations.md).

## Context
A prompt was text only: its body `{text, attachments}` reserved `attachments` and required it empty
([03-api.md](../spec/03-api.md) §10). People want to hand an agent a screenshot, a log file, a PDF or a spreadsheet
together with the instruction. A prompt event is at most 256 KiB of ciphertext (E8), too small for a screenshot in full
quality, and the runner hands a prompt to Claude Code by typing it into its terminal, which cannot carry a file.

Three ways were weighed with the owner: shrinking images into the prompt event (A), sealing every attachment as an
object of its own at the hub (B), or A first and B later (C). The owner chose B, for files and images alike.

## Decision
1. **Attachment object.** Every attached file is one object at the hub, sealed by the browser under a key derived from
   the current workgroup key: `k_att = HKDF(WK_e, salt, P‖"attachment\0"‖object_id)` with 32 random bytes of salt per
   object; the file is split into pieces of at most 1 MiB, each sealed with AES-256-GCM and the AAD
   `P‖"attachment-chunk\0"‖canonical({"index","object"})` ([04-crypto.md](../spec/04-crypto.md) §4.14). At most
   32 MiB per file, at most 10 files per prompt, a file has at least one byte.
2. **The prompt names and binds them.** The prompt body lists each attachment as
   `{object, name, type, bytes, epoch, salt, digest}`: the file name and media type exist only inside the prompt's
   ciphertext; `digest` is the SHA-256 over the SHA-256 of every sealed chunk in index order, so the author's signature
   on the prompt covers the exact ciphertext. No manifest and no signature of its own: whoever can read the prompt has
   everything to check and open the attachment, and the hub can neither swap, cut nor extend one.
3. **Hub.** A member with `wg:write` uploads the chunks (`PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}`)
   and commits the object with `{chunks, size, digest}` (`POST …/commit`), which the hub checks against what it stored;
   members with `wg:read` and devices assigned to the workgroup read the chunks of a complete object. The hub sees the
   object ID, the workgroup, the uploading user, the number and length of the chunks and the times – never the name,
   the type or the content ([04-crypto.md](../spec/04-crypto.md) §7). The sealed bytes of every attachment of an
   organization stay within `AGENT_HUB_ATTACHMENT_ORG_BYTES`; an upload without a new chunk for 6 hours is removed by
   the job `attachments.expire`; complete objects stay with their workgroup.
4. **Browser.** The composer takes files by a button, by drag and drop and by pasting an image; each is uploaded and
   committed when the prompt is sent, before the prompt is sealed, so a prompt never names an object the hub does not
   hold complete (a prompt still needs a text). The chat shows the attachments of every prompt: images (PNG, JPEG,
   GIF, WebP) as a preview opened from the decrypted bytes (a `data:` URL; the CSP stays as it is), every other file as
   a named entry that downloads the decrypted file.
5. **Runner and session.** Before a prompt with attachments is typed, the runner downloads every chunk, checks count,
   size and digest against the verified prompt, decrypts with `WK` of the named epoch and imports the files into the
   session's workspace under `attachments/<event id>/<n>-<name>` (the guest's file import, which refuses links,
   protected names and paths outside the workspace; the name is reduced to `[A-Za-z0-9._-]` and numbered, so two files
   of one prompt never collide and no file takes a protected name). The typed text is the prompt's text followed by
   one line per file with its path, media type and size, so the agent opens them with its own tools (images too). The
   first prompt of a new task with attachments does not go on the command line of the start: the session starts
   without it and the regular delivery imports the files and types it, so the files are in place before the agent
   reads the prompt. An attachment that cannot be read (missing, wrong digest, not decryptable) refuses the prompt
   like an unreadable one (`prompt_unreadable`); a hub or guest that cannot be reached is retried.

## Consequences
- Contracts: `body_prompt.attachments` items get a schema (additive: an empty list stays valid); new vectors
  `contracts/vectors/attachment.json`; a change note under `contracts/changes/`.
- New hub tables `attachment_object` and `attachment_chunk` (with backup rules and column classes), new routes, the
  quota setting and the expiry job.
- Three implementations of §4.14 (hub reference, runner copy, web) pass the same vectors.
- Readers that predate this decision refuse a prompt with attachments as a body they do not know; the hub, the runner
  and the web client are released together.
- The agent tool sends text prompts as before; attaching files with it follows in a package of its own (parity with the
  web client, [E-04](../KONZEPT.md#e-04)).

## Alternatives considered
- **Images shrunk into the prompt event (A).** Rejected by the owner: quality loss for screenshots with text, and no
  room for other files.
- **A signed manifest per attachment like a checkpoint.** Not needed: the signed prompt already carries every value a
  manifest would sign, and a second signature would only duplicate it.
- **Binding the object to the task.** A new task's ID exists at the hub only after its first prompt, which must already
  name the uploaded objects; the workgroup is the scope that exists before and after.
