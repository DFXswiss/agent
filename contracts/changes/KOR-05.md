# KOR-05 – contract changes

ADR-027 section 3 (additive).

## `outbox/objects.json#/$defs/body_quota`: the form `unavailable`

05-runner.md section 10.6 makes quota polling feature-detected: the first `401`/`403` of the usage endpoint marks the
owner's subscription credential `quota_unavailable`, and the web shows "not available". The runner could not tell the
hub, so an older snapshot of the windows stayed and the web kept showing it (audit finding B-18). `body_quota` now has
two forms (`anyOf`): the windows as before (unchanged, at least one window), or `{owner_id, unavailable: true,
fetched_at}` without windows. The hub drops the owner's windows fetched at or before such a report, so
`GET /v1/me/quotas` answers no rows ("not available") until a later fetch reports windows again. Fixtures:
`fixtures/outbox/quota/valid/unavailable.json`, `invalid/unavailable-false.json`, `invalid/unavailable-with-windows.json`.
