# HOT-26 – contract changes

ADR-027 section 3 (additive). A renewal of a Claude subscription revokes the former access token at once; a session
whose request was refused with it (`401`) is now continued by the runner as soon as a valid access is there (ADR-092
section 6, ADR-081 section 6). The task shows a quiet line for it, read from the audit like a model switch.

## `audit/session.model_switched.json`, `outbox/objects.json` (`audit` detail): cause `renewed` (additive)

- `renewed`: the runner continued a main session whose access the provider refused, on the renewed access of the
  same subscription or on another usable one (`from_access` and `to_access` may be equal; `until` is null; the models
  are the session's model). Metadata only, like every other cause. The hub creates no notification for it.

- `model_unavailable`: a main session's subscription refused its model ("Usage credits are required for this model")
  and no subscription with access to that model is free; the session waits on its model (`from_access` and
  `to_access` the current access, the model on both sides, `until` null). The task shows "no subscription with access
  to <model> free"; the hub notifies the task's owner like a switch.

## `guest/journal/record.json`: `usage.refusal` (additive)

- `refusal: "model_not_in_plan"`: the provider's error answer says the subscription does not cover the requested model.
  Written by the guest gateway; the runner then treats the access as refused for that model, not as used up (no
  deferral, no exhaustion streak). Roll-out: runners first (an older runner ignores the field), then the guest image.

## `api/device-state.response.json`: `model_fallback.api` 2 (additive)

A hub that accepts the causes `renewed` and `model_unavailable` sends `model_fallback: {api: 2}`; a runner sends them
only to such a hub (`api >= 2`). Towards a hub with `api: 1` the session is continued or waits all the same, without
the audit row.

Fixtures: new valid `audit/session.model_switched/{renewed,model-unavailable}.json`,
`outbox/audit/model-switched-renewed.json`, `guest/journal/usage/model-not-in-plan.json`; new invalid
`guest/journal/usage/unknown-refusal.json`.
