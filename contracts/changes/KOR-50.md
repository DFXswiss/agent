# KOR-50 – contract changes

ADR-027 section 3 (additive); design: ADR-080 (model access with fallback, visible quotas and DFX access on own
hardware).

## Outbox (runner → hub), sent only to a hub whose device state carries `model_fallback.api >= 1`

- `enums/outbox-item-kinds.json` ends with `operator_quota`, `dfx_key_state`, `dfx_key_issued`, `dfx_key_usage`;
  `outbox/<kind>.json`, `outbox/objects.json#/$defs/body_<kind>` and their alternatives in `$defs/item`:
  - `operator_quota` `{at, subscriptions (≤ 64): [{name, state: usable|exhausted|blocked|deferred|ignored, readable,
    windows (≤ 16, `quota_window`), limited_at, until, fetched_at}]}` – the complete snapshot of a platform device's
    operator subscriptions (file names, never a token); refused from an org device (`403 forbidden`).
  - `dfx_key_state` `{issuer}` – a platform device holds a management key of the model provider; refused from an org
    device.
  - `dfx_key_issued` `{request_id, org_id, device_id, key_ref (^[A-Za-z0-9_-]{8,128}$), envelope (canonical b64u,
    ≤ 8192 characters)}` – the hub stores the envelope as an opaque string.
  - `dfx_key_usage` `{key_ref, org_id, device_id, spent_micro_usd (count), at}` – the provider's **cumulative** spend.
- `body_quota` (both forms) gains the optional `limited: {at, until: time|null, readable}` (`$defs/quota_limited`);
  the window object is now the named definition `$defs/quota_window` (unchanged).
- `body_session_transition` (all states but `failed`) gains the optional `wait_until: time|null` and `dfx_choice:
  boolean`.
- `session_failure_reason` (`enums/session-failure-reasons.json`, `common.json`): `model_quota_exhausted` (appended).
  It is also the reason (`error_code`) of a `suspended` transition of a paused session.
- Outbox audit action `session.model_switched` (`enums/outbox-audit-actions.json`, `enums/audit-actions.json`,
  `audit/session.model_switched.json`, the `body_audit` alternative): `{from_access, to_access: subscription|api_key|
  platform, from_model, to_model: model id|null, cause: exhausted|unusable|released, until: time|null}`.

- `audit/outbox.rejected.json`: `item_kind` accepts the four new kinds (a rejected item is audited like every other
  kind).

## Device state (`GET /v1/device/state`, active form)

- `model_fallback: {api: 1}` – always sent by a hub of this release, optional in the schema for older hubs.
- `sessions[]`: optional `fallback: {own_to_dfx, operator_to_platform}` (the owner's settings; absent: false/true)
  and `model_consent` (the task's consent; absent: false).
- `dfx_keys: {requests (≤ 1000): [{request_id, org_id, device_id, device_kem (b64u_32)}], keys (≤ 10000):
  [{key_ref, org_id, device_id, headroom_usd (number ≥ 0), disabled, revoke}]}` – only for the elected issuer (the
  online active platform device with the smallest ID whose last `dfx_key_state` said `issuer: true`).
  A key with `revoke: true` stays listed until the issuer reports a `dfx_key_usage` of it with `at` at or after the
  revocation (its final spend, booked); then the hub forgets it. The issuer deletes the key at the provider after that
  report.
- `dfx_key_held (≤ 1000): [{org_id, key_ref, envelope, issuer_device_id}]` – only for an org device, one per
  organization it serves.

## API

- `api/objects.json`: `fallback_settings {own_to_dfx, operator_to_platform}`; `settings` gains the optional
  `fallback` (a hub of this release always sends it); `attention_row` gains the optional `wait_until: time|null` (only
  `quota_wait` / `quota_choice` records carry it; the contract test allows exactly this one optional field).
- `PUT /v1/me/settings`: optional `fallback`; without it the stored fallback settings stay. `audit/settings.put.json`
  gains the optional booleans `own_to_dfx` and `operator_to_platform`.
- `POST /v1/tasks/{task}/model-consent` (`task-model-consent`, right `task:write`, section 4.6, owner KOR-50):
  request `{}`, response `{task_id, model_consent: true}`; audit `task.model_consent` `{changed}`
  (`audit/task.model_consent.json`).
- `GET /v1/orgs/{org}/quotas` (`org-quotas`, right `org:member`, section 4.3, read resource `org.quotas`, audit
  `read.org.quotas` with empty detail): `{own: {windows (as GET /v1/me/quotas), unreadable: {since}|null, limited:
  {at, until, readable}|null}, operator: {granted, subscriptions (≤ 1000): [{label, state, readable, used_percent:
  number|null, resets_at, limited_at, until, fetched_at}]}, platform: {available}, summary: {usable, exhausted,
  next_release: time|null}}`. Endpoint table `tests/platform/contracts/spec/endpoints-kor50.json`.
- `enums/attention.json`: `needs_decision/quota_choice` and `working/quota_wait` (appended to their categories).

Path parameters follow the existing convention of `api/INDEX.json` (`{task}`, `{org}`).

## Compatibility

Hub first. An older hub refuses the new item kinds, the `limited` field, the wait fields and the new audit action
(`request_invalid`); the runner therefore sends them only when the device state carries `model_fallback.api >= 1`. An
older runner ignores the new device-state fields and never sends the new items. An older web shows an unknown
attention reason with its category's generic text and ignores `fallback` and `wait_until`.

## Notification kind `model.switched`

`api/objects.json` `notification_row.kind` gains `model.switched` (ADR-080 section 5, ADR-020): when the hub stores
the outbox audit `session.model_switched`, the task's owner (while they read the task's workgroup) gets a
content-free notification with `org_id`, `wg_id`, `task_id` of the session's task and `ref` =
`<from_access>:<to_access>:<until or empty>` (a new alternative of `ref`). Texts are the web's (code + metadata
only). An older web shows an unknown kind with its generic text.
