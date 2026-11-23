# ADR-031: Budget check of operation registration as an extension point

Status: Accepted (2026-11-22)

## Context
03-api.md §8.3 check H4 includes the platform budget ("subagents: depth, parallel, total; budget"), and §2 names
`AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY`. The registration of operations is built by HUB-12 (`agent_hub.operations`),
the budget computation by a later package (HUB-14). ADR-027 §4 forbids editing another package's files and lists the
discovered extension points of the hub; it has none for a budget decision. Without one, HUB-14 would have to edit the
operations area or the registry.

## Decision
- The operations area asks `agent_hub.operations.limits.budget_check(BudgetRequest)` with `connection` (the
  registration's transaction, task row held), `org_id`, `session_id` (the requesting session) and `now`; `True` means
  the org may start more model work.
- The implementation is discovered as `agent_hub/<area>/budget.py:CHECK`, a callable `CHECK(request) -> bool`
  (same discovery rules as ADR-027 §4: sorted areas, a module without the attribute is skipped). At most one area may
  define it (two are a `RegistryError` at the first registration after start); a non-callable value is refused.
- Without a registration the budget is unlimited.
- In v1 only `subagent.request` consults the budget (it is the only operation type that starts model work); a refusal
  is `denied` with `budget_exceeded`, checked after the subagent limits of ADR-017.

## Consequences
- HUB-14 adds one file and needs no change in the operations area; tests install a fake with `limits.configure`.
- The check runs inside the registration's transaction and must be fast and must not take locks other than reads.

## Alternatives considered
A signal (asynchronous, cannot refuse), a configuration value read by the operations area (the computation needs usage
data owned by HUB-14), editing the registry's list of extension points (central list).

## Deviation from DESIGN.md
None beyond ADR-002.
