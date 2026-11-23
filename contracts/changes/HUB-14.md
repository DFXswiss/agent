# HUB-14 – contract changes

## Stream frame `session`: optional `usage` (additive)

The package asks for the `session` frame to carry the usage counters, so that task readers see them live
(KONZEPT §6, `03-api.md` section 5 "session row + newest transition"). The frame data had no place for them.

- `ws/sse/objects.json` `$defs.data_session`: new optional property `usage` (`api/objects.json#/$defs/usage` or
  `null`). The hub sends it in the frame it publishes after every accepted `usage` outbox item; frames of state changes
  do not carry it (clients keep the last counters they saw, or read `GET /v1/sessions/{session}`).

Producers that do not send the field stay valid; consumers that ignore it are unaffected.

Fixtures: `fixtures/ws/sse/session/valid/with-usage.json`, `fixtures/ws/sse/session/invalid/usage-negative-cost.json`.
