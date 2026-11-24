# KOR-40 – contract changes

ADR-027 section 3 (additive). Decisions: ADR-063 (audit), ADR-064 (guest).

## Audit actions (`enums/audit-actions.json`, `audit/*.json`, fixtures)

New actions with their detail schemas:

| Action | Written by | Detail |
|---|---|---|
| `request.refused` | request pipeline: a refused write whose route left no refusal row | `{method, route, status, code}` |
| `request.write` | request pipeline: a successful write without a row of its own (generic routes, unchanged repeats) | `{method, route, status, code: "ok"}` |
| `audit.repeat` | the windowed log: repeats of a read key or of a pipeline row in one window | `{action, repeats, first_seq, window_s}` |
| `read.stream` | `GET /v1/stream` (now with `read_resource: "stream"` in `api/INDEX.json`) | empty |
| `read.stream.terminal` | each session whose terminal output a stream watches | empty |
| `event.append_batch` | audit job: the session's own events of one session and scope | `{scope, count, first_scope_seq, last_scope_seq, ct_bytes}` |
| `settings.put` | `PUT /v1/me/settings` | `{model_access, model_id, locale}` (the new values) |
| `auth.passkey_bundle` | `PUT /v1/me/bundles/passkey:<credential>` | empty (target: the passkey) |
| `wrap.put` | `POST /v1/wraps`, `POST /v1/device/wraps` | `{stored, ignored}` |
| `agent.pairing` | `POST /v1/agent-pairings` (anonymous) | empty (target: the agent) |

`auth.recovery` gets the step `rotate_begin` (`POST /v1/me/recovery/begin`); `notification.read` is written for
every marking, not only an agent's (description changed, schema unchanged). The rule of `api/INDEX.json` changes from
"a streaming GET has no `read_resource`" to "a streaming GET may have one".

## Event kind `session.error` (`enums/event-kinds.json`, `events/kinds.json`, `events/session.error.json`)

Device-authored, workgroup scope, right `device_assigned_wg_session`, header like the other session kinds. Body
`{code}` with the closed code list of the guest journal's `guest.error` record (`objects.json#/$defs/body_session_error`
refers to `guest/journal/record.json#/$defs/body_guest_error`). `common.json#/$defs/event_kind` lists it.

## Guest and session reasons

- `enums/session-failure-reasons.json` and `common.json#/$defs/session_failure_reason`: `hook_unavailable` (the guest
  ended the session because a decision hook failed, ADR-064 section 2).
- `guest/journal/record.json` `guest.error` codes: `turn_unconfirmed` (the gateway refused an agent-loop request
  outside a confirmed turn, ADR-064 section 1).

## Compatibility

Hub first: an older hub refuses the event kind `session.error` (`kind_not_allowed`); the runner sends it only after
the hub knows it (the outbox rejects the item and moves on, so an early runner loses nothing else). An older web drops
unknown kinds and shows an unknown failure reason with the generic text. New audit actions appear in an older web's
audit list under their action name.
