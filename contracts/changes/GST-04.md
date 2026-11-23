# GST-04 – contract changes

ADR-027 section 3 (defect fixes and additive changes). The guest model gateway (`05-runner.md` section 10.2) is the
only consumer; no producer exists yet (RUN-04/RUN-12 come later).

## `guest/session-credential.request.json`: access kind, model allow-list, daily budget (defect)

Section 10.2 makes the gateway inject the credential **per access kind** (the subscription appends `oauth-2025-04-20`
to `anthropic-beta`), refuse models outside the **catalog allow-list** of the kind (`400 model_not_allowed`), refuse
requests over the **daily platform budget** (`429 budget_exceeded`, limit from `device/state` `platform_budget`) and
journal `usage.context_window_tokens` (the catalog `context_window` of the model, see the `usage` fixtures). The
provisioning body `PUT /v1/sessions/{id}/credential` (03 section 7.1) carried none of these, and `anthropic-beta` is a
reserved header name, so the runner could not express the subscription header either. The gateway runs in the API
process, which never sees the `POST /v1/sessions` body.

- new required `access` (`common.json#/$defs/model_access`): selects the injection.
- new required `models_allowed`: ≤ 64 `{model_id, context_window}` – `model-catalog.json` `access.<kind>.allowed`
  with each model's `models.<id>.context_window`; empty refuses every model.
- new optional `budget` `{usd_per_day, spent_usd}` (numbers ≥ 0): absent = unlimited. The gateway adds the cost it
  observed since the provisioning (OpenRouter `usage.cost`) to `spent_usd` and refuses once the sum reaches
  `usd_per_day`; the runner re-provisions with fresh values (RUN-12).

## `guest/journal/record.json`: `usage.provider_outside_allowlist`, `guest.error` `credential_missing` (additive)

- `usage.provider_outside_allowlist` (optional, `const: true`): the response's `provider` is outside the provisioned
  `provider_allowlist`. The request is not blocked; the runner turns the record into the audit
  `model.provider_outside_allowlist` (05 section 10.2) with the session and provider of the same record.
- `guest.error.code` `credential_missing` (with the session ID): an authenticated model request found no provisioned
  credential (never provisioned, lost by a restart of the API process). The runner re-provisions or ends the session
  with `model_access_unavailable`.

Fixtures: every existing credential fixture carries the new required fields; new valid `subscription`,
`platform-budget`, `usage/provider-outside-allowlist`, `guest.error/credential-missing`; new invalid
`missing-access`, `missing-models_allowed`, `access-unknown`, `budget-negative`, `budget-missing-spent`,
`model-id-invalid`, `model-without-window`, `context-window-zero`, `usage/provider-outside-allowlist-false`.
`tests/platform/contracts/test_ctr02.py` lists the new fields next to the specified ones.
