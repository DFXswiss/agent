# HUB-16 – contract changes

## `stale` in the monitoring overview (additive)

The package asks for the "stale" mark of 08-monitoring.md section 6 (values older than 60 s are greyed with their
age) as a field of the overview. `api/monitoring-overview.response.json`: every item of `devices`, `vms` and
`sessions` gets the optional boolean `stale` - true when `latest` is null or its `ts` is more than 60 s older than the
hub clock. The hub always sends it; it is optional so that existing producers and fixtures stay valid. Fixtures
`fixtures/api/monitoring-overview.response/valid/stale.json` and `invalid/stale-not-boolean.json`.
