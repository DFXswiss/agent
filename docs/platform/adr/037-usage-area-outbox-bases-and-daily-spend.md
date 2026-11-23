# ADR-037: Outbox base handlers claimed by other areas; daily platform spend per device

Status: Accepted (2026-11-22)

## Context
- ADR-030 lets only the outbox area register the base handler of an outbox item kind; other areas can only extend a
  kind. HUB-14 owns the hub side of usage and quotas (KONZEPT §6, 03-api.md §6.1) and takes the `usage` and `quota`
  handlers out of the outbox area. ADR-027 §4 forbids a central list naming the areas.
- R-P.13 needs today's platform spend per organization (`PLATFORM_BUDGET_USD_PER_ORG_DAY`). 02-data-model.md §2.8 has
  per-session counters only. A push is one transaction (ADR-030); one row per organization and day would be locked by
  every device that pushes usage for that organization until its commit, and two devices pushing usage of two
  organizations in opposite order could deadlock.

## Decision
- An area claims base handlers with `<area>/outbox.py:BASE_KINDS` (a collection of item kinds), discovered like
  `HANDLERS` (`agent_hub.outbox.chains`). A claimed kind's base is the claiming area's handler; each kind may be claimed
  by one area only, never by the outbox area itself; the outbox area must not register a claimed kind. All other rules
  of ADR-030 stay (extensions after the base in sorted area order, every kind needs a base, broken registrations stop
  the start). The usage area claims `usage` and `quota`.
- New table `hub.usage_daily(org_id, day, device_id, cost_micro_usd)` (migration 0006), primary key
  `(org_id, day, device_id)`: the `usage` handler adds the cost delta of platform work to the row of the session's
  org, the UTC day of the hub's receive time and the pushing device. The spend of an org is the sum of its rows.
  Pushes of one device are serialized by its outbox stream, so no two transactions write the same row concurrently.

## Consequences
- No central list; a later area can claim a kind by adding one attribute.
- Readers sum a handful of rows per org (one per device that served the org that day).
- `usage_daily` is an addition to 02-data-model.md §2.8 (not a projection; past days stay as operator history).

## Alternatives considered
A hard-coded kind → area map in the outbox area (central list), keeping the inserts in the outbox area with an
extension in the usage area (the base would still own usage semantics), one row per org and day (hot row, lock-order
deadlocks between devices), summing `usage_counter` per day (counters are per session, not per day).

## Deviation from DESIGN.md
None.
