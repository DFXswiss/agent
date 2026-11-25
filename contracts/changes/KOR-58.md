# KOR-58 – contract changes

ADR-027 section 3 (additive: every message that was valid stays valid). Device failure (ADR-087): the active sessions
of a device that stays away beyond `AGENT_HUB_DEVICE_FAILOVER_SECONDS` are interrupted, their tasks move to another
suitable device and continue there from the last synchronised checkpoint.

## Enums

- `enums/session-failure-reasons.json` (and `common.json#/$defs/session_failure_reason`): `device_failed` - the hub
  ended the session `stopped` with this reason when it moved the task to another device.
- `enums/task-states.json`: scheduling reason `device_failed` with the new scheduling category `interrupted` (the task's
  main session is interrupted because its device failed; it continues there or on another device by itself).
- `enums/attention.json`: reason `device_failed` in the category `blocked` (workgroup-wide like `device_unreachable`).
- `enums/operation-error-codes.json` (and `common.json#/$defs/operation_error_code`): `device_failed` - a test run the
  hub ended because the device that ran it failed.
- `enums/audit-actions.json`: `session.interrupted` (detail `audit/session.interrupted.json`: `offline_since`) and
  `session.interruption_ended` (empty detail), both written by the hub.

## Browser API

Producer: the hub; consumers: web, `agent-platform`.

- `api/objects.json#/$defs/session_row`: optional `interrupted_at` (time or null) and `failover_of` (session ID or
  null). Older hubs leave them out; a client treats a missing field as null.

## Device API

Producer: the hub; consumer: the runner. Old runners ignore both fields.

- `api/device-state.response.json` (active device): optional `failover_seconds` (integer) and `failovers` (array of
  `{session_id, from_session, cancelled_runs}`): the live main sessions of this device that continue a session of a
  failed device. The runner does not wait for a final checkpoint of `from_session` and tells the session that it
  continues after a device failure.
