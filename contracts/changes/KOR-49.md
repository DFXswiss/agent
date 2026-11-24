# KOR-49 – contract changes

ADR-027 section 3 (additive); design: ADR-079 (no approval as a precondition).

- `common.json`: platform-log bodies `platform_body_platform_policy` (`name` ∈ `open_platform_access`,
  `open_platform_invites`, `on` bool) and `platform_body_org_operator_subscriptions` (`org`, `granted` bool) in
  `platform_entry_body`; `platform_role` gains `operator`. Schemas `log/platform/platform_policy.json` and
  `log/platform/org_operator_subscriptions.json` with fixtures (an unknown switch name and a non-boolean value are
  invalid; `role_grant` of `operator` is valid). Older readers stop at the new types (`log_type`); the hub refuses them
  (`409 peer_outdated`) until every active device announced `log.policy`.
- `vectors/platform-log.json` (and the logs embedded in the other log vectors): six more entries (both switches, the
  list, the role `operator`), the projections gain `policies` and `operator_subscriptions`, further `platform_access`
  cases (an org without its own entry follows the switch) and negative vectors (a switch or list entry signed by the
  hub key: `log_signature`; `operator` granted twice: `log_rights`).
- `enums/audit-actions.json` and `audit/platform.platform_policy.json`, `audit/platform.org_operator_subscriptions.json`
  (`{seq}`).
- `api/objects.json` `notification_row.kind`: `operator.request` (`org_id` null, `ref` = `<reason>:<org id>`) and
  `device.needed` (`ref` = the workgroup).
- `api/platform-waiting.response.json`: the reason `credit_exhausted`.
- `ops/rules.json` `protected_branches.default`: `[]` (no platform-side default, interpretation A17).
