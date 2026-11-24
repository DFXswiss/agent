# KOR-33 – contract changes

ADR-027 section 3 (additive). Real session resume (ADR-059): context checkpoints are exported from the guest,
encrypted by the runner, stored at the hub and restored into a new sandbox on any VM.

## Guest

Producer and consumer: the guest (agent `agent_guest`) and the runner (`agent_runner`); the hub never sees these
messages.

- New endpoint `POST /v1/sessions/{id}/context/export` (`guest/session-context-export.request.json`,
  response_kind `tar`): body `{workspace_max_bytes, transcript_max_bytes}`; the answer is the context archive v1 of
  ADR-059 section 2. `guest/session-context-export.response.json` describes its first member `context.json` (like the
  manifest member of `files/export`); `transcript/<path>` and `work/<path>` members follow.
- New endpoint `POST /v1/contexts/{id}/stage` (`guest/context-stage.request.json`, request_kind `query+tar`, no
  query parameters; body a context archive of at most 16 GiB; every line of every transcript `.jsonl` must be
  complete JSON, else `400 request_invalid`) -> `guest/context-stage.response.json` `{files, bytes}`.
- `guest/sessions-create.request.json`: optional `restore {claude_session_id, object_id}` - the new sandbox is
  created from the staged context and the agent resumes `claude_session_id`. Not together with
  `resume_claude_session_id` (`400 request_invalid`, a rule the schema does not state); without a matching staging
  `409 state_conflict`. No new error code.

Fixtures: `guest/session-context-export.{request,response}/`, `guest/context-stage.{request,response}/`,
`guest/sessions-create.request/valid/restore.json`, `.../invalid/restore-without-object.json`.
`tests/platform/contracts/test_ctr02.py` lists the two endpoints and the `restore` object.

## Hub API

Producer: the hub (`agent_hub.contexts`); consumers: the runner (device routes, device state, outbox audit) and the
web and the user's agent (task list, session rows). All additive; old consumers are unaffected.

- Device endpoints (`api/INDEX.json`, owner KOR-33, `tests/platform/contracts/spec/endpoints-kor33.json`):
  `PUT /v1/device/contexts/{object}/chunks/{index}` (`device-context-chunk-put`), `POST
  /v1/device/contexts/{object}/commit` (`device-context-commit`), `GET /v1/device/contexts/{object}`
  (`device-context-get`), `GET /v1/device/contexts/{object}/chunks/{index}` (`device-context-chunk-get`), `POST
  /v1/device/pr-links` (`device-pr-links-post`). Browser and agent endpoints: `GET /v1/tasks/{task}/contexts`
  (`task-contexts`, read resource `task.contexts`) and `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`
  (`workgroup-pr-link`, read resource `workgroup.pr_link`). New `common.json` definitions: `q_index` (chunk index
  path text), `context_manifest` (04-crypto.md section 4.11), `context_fallback_reason`, `context_reason`,
  `context_workspace`.
- Error code `quota_exceeded` (413) with the additive error detail `quota {scope: object|org, limit, used}`.
- `api/objects.json#/$defs/session_row`: optional `context_restore` (`restored`|`fallback`|null) and `context_reason`
  (`common.json#/$defs/context_reason`: the fallback reasons, and for `restored` `workspace_partial` or
  `workspace_omitted` when the workspace did not come back whole; null otherwise). The hub always sends both.
- `api/device-state.response.json`: a main session item may name `context {object_id, session_id, created_at,
  size}`, the newest complete checkpoint of the task (newest = the checkpoint of the newest main session by its
  creation at the hub, then the newest object ID); `session_id` lets the runner tell whether it belongs to
  `resume_from` (the final checkpoint of that session) or to an earlier one.
- Audit actions (`enums/audit-actions.json`, `audit/<action>.json`):
  - `session.context_saved {chunks, size, pruned}` (hub, actor device, on commit);
  - `session.context_refused {reason: quota_exceeded, scope: object|org, limit, used, requested}` (hub, actor device,
    `result` `denied`, target the object): the hub refused a chunk for a quota; written in its own transaction, so
    it stays although the upload was refused;
  - `session.context_restored {object_id, workspace: full|partial|omitted|empty, stale}` and
    `session.context_fallback {reason}` (runner, outbox item `audit`, `enums/outbox-audit-actions.json`). The hub
    stores the outcome on the session row: restored with `workspace` `partial`/`omitted` becomes `context_reason`
    `workspace_partial`/`workspace_omitted`. `stale` is true when the restore used an older checkpoint because the
    final one of `resume_from` did not arrive in time (review 1 decision 1).
  - Read actions `read.device.context`, `read.device.context.chunk`, `read.task.contexts`,
    `read.workgroup.pr_link`.
- The audit detail checker accepts `boolean` leaves (`stale`; the generic detail form already allowed booleans).
- Vectors: `vectors/context.json` (key derivation, chunk sealing, manifest signature and digest, pull-request tag).

Fixtures: `api/<name>.{request,response}/` of the endpoints above, `api/device-state.response/{valid/context,
invalid/context-*}`, `api/session-get.response/{valid/context-*, invalid/context-*}`, `api/error.response/
invalid/quota-scope-unknown.json`, `audit/session.context_{saved,refused,restored,fallback}/`,
`outbox/audit/{valid/variant-19,variant-20, invalid/context-*}`.
