# KOR-46 – contract changes

ADR-027 section 3 (additive: every message that was valid stays valid). Shared sessions (ADR-083): the session
learns who speaks, the consequences of an instruction name the person who gave it, the queue of a task's prompts is
visible and a waiting prompt can be withdrawn by its author.

## Guest and hooks

Producer: the runner (registration) and the guest's hook endpoint (part of the `UserPromptSubmit` decision, `prompt.check`); consumers:
the guest and Claude Code. The hub never sees these messages.

- `guest/session-prompts.request.json`: optional `speaker` (`guest/objects.json#/$defs/speaker_line`, one line
  starting with `agent.dfx.swiss instruction from: `, at most 2048 characters) - the sender line the runner composed
  from the verified author of the prompt. An older guest answers `400 request_invalid`; the runner then registers the
  prompt again without it.
- `mcp/objects.json`: new `$defs/speaker_context` (the sender line, optionally followed after a blank line by a
  session notice and/or a ping notice); `hook_context` accepts it too - a widening.

Fixtures: `guest/session-prompts.request/{valid/speaker,invalid/speaker-two-lines,invalid/speaker-other-prefix}.json`,
`mcp/hooks/UserPromptSubmit.response/{valid/speaker,valid/speaker-and-session-notice,invalid/speaker-second-line}.json`.

## Device API, hints and outbox

Producer: the hub (device state, hints) and the runner (registration, outbox); all fields optional.

- `api/device-state.response.json`: optional section `prompt_queue {via_agent, withdrawn}` (event IDs of pending
  prompts of this device's sessions: received from the author's agent; whose author asked to withdraw them). A hub
  that sends the section takes `triggered_by` and the outbox audit `session.prompt_withdrawn`; the runner sends
  neither to a hub without it.
- `ws/device/objects.json#/$defs/hint_prompt`: optional `via_agent` (boolean).
- `api/device-operations.request.json`: optional `triggered_by` (UUID or null) in both variants.
- `outbox/objects.json`: optional `triggered_by` (UUID or null) in `body_usage` and `body_runtime`; new `body_audit`
  variant `session.prompt_withdrawn` (target the prompt event, empty detail).
- `enums/outbox-audit-actions.json`: `session.prompt_withdrawn`.

Fixtures: `api/device-state.response/{valid/prompt-queue,invalid/prompt-queue-extra-field,invalid/prompt-queue-missing-withdrawn}.json`,
`ws/device/prompt/{valid/via-agent,invalid/via-agent-text}.json`,
`api/device-operations.request/{valid/triggered-by,valid/triggered-by-null,invalid/triggered-by-invalid}.json`,
`outbox/usage/{valid,invalid}/triggered-by*.json`, `outbox/runtime/{valid,invalid}/triggered-by*.json`,
`outbox/audit/{valid/prompt-withdrawn,invalid/prompt-withdrawn-detail}.json`.

## Browser and agent API

Producer: the hub; consumers: the web and the user's agent (`agent-platform`).

- New endpoints (`api/INDEX.json`, owner KOR-46, `tests/platform/contracts/spec/endpoints-kor46.json`):
  `GET /v1/tasks/{task}/queue` (`task-queue`, `task:read`, read resource `task.queue`) → `{task_id, prompts:
  [queue_prompt]}`, and `POST /v1/tasks/{task}/prompts/{event}/withdraw` (`prompt-withdraw`, `task:write`, only the
  prompt's author) → `{state: withdrawn|withdrawing}`. New `api/objects.json` definitions `queue_prompt`
  `{event_id, author_id, via_agent, scope_seq, received_at, session_id, device_id, state:
  waiting|waiting_for_device|withdrawing}` and `charge_person`.
- `api/objects.json`: optional `triggered_by` (UUID or null) in `operation_row` and `session_row`; notification kind
  `prompt.waiting_for_device` (`ref` the prompt event ID).
- `api/org-billing.response.json`, `api/task-charges.response.json`: optional `people` (`charge_person`: consumption
  per person, `user_id` null for unattributed consumption).
- Audit actions (`enums/audit-actions.json`, `audit/<action>.json`): `prompt.withdraw {state}` (hub, actor the
  author, target the prompt event), `session.prompt_withdrawn {}` (from the runner's outbox), `read.task.queue`;
  every `operation.*` detail takes an optional `triggered_by`.

Fixtures: `api/task-queue.{request,response}/`, `api/prompt-withdraw.{request,response}/`,
`api/operation-get.response/{valid,invalid}/triggered-by*.json`, `api/session-get.response/valid/triggered-by.json`,
`api/{task-charges,org-billing}.response/{valid/people,invalid/people-extra-field}.json`,
`api/notifications-list.response/valid/waiting-for-device.json`, `audit/{prompt.withdraw,session.prompt_withdrawn,read.task.queue}/`,
`audit/operation.accepted/{valid,invalid}/triggered-by*.json`.
