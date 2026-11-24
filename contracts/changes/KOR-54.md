# KOR-54 – vectors of the key model

Additive, vectors only (no schema, endpoint or format changes). The key model (`docs/platform/key-model.md`) names the
shared vectors of every rule; three rules had none:

- `vectors/log-wraps.json`: negative `org_key_for_device_without_workgroup` (KM-10, ADR-007) with the positive control
  `W2_org_key_for_device_with_workgroup`; negative `principal_outside_log_is_no_recipient` (KM-06); positive
  `cred_device_of_org_with_consent`, for which the file now also carries the log `admit` of `log.json`.
- `vectors/log.json`: negative `cred_device_of_org_without_consent` (KM-15, ADR-060).

Every implementation runs them through the existing operations `check_wrap` and `check_cred_wrap`; no consumer list
changed.
