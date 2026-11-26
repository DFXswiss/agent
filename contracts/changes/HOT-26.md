# HOT-26 – contract changes

ADR-027 section 3 (additive). A renewal of a Claude subscription revokes the former access token at once; a session
whose request was refused with it (`401`) is now continued by the runner as soon as a valid access is there (ADR-092
section 6, ADR-081 section 6). The task shows a quiet line for it, read from the audit like a model switch.

## `audit/session.model_switched.json`, `outbox/objects.json` (`audit` detail): cause `renewed` (additive)

- `renewed`: the runner continued a main session whose access the provider refused, on the renewed access of the
  same subscription or on another usable one (`from_access` and `to_access` may be equal; `until` is null; the models
  are the session's model). Metadata only, like every other cause. The hub creates no notification for it.

## `api/device-state.response.json`: `model_fallback.api` 2 (additive)

A hub that accepts the cause `renewed` sends `model_fallback: {api: 2}`; a runner sends the cause only to such a hub
(`api >= 2`). Towards a hub with `api: 1` the session is continued all the same, without the audit row.

Fixtures: new valid `audit/session.model_switched/renewed.json`, `outbox/audit/model-switched-renewed.json`.
