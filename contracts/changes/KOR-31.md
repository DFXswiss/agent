# KOR-31 – contract changes

ADR-027 section 3 (additive), ADR-062, `docs/platform/spec/12-billing.md`.

## Outbox item kind `runtime` (new), `usage` gains three optional fields

`enums/outbox-item-kinds.json` ends with `runtime`; `outbox/runtime.json` and `outbox/objects.json#/$defs/body_runtime`
(`{session_id, vm_id, vm_kind: "session"|"work", size, from, to, runtime_ms}`, all required, `runtime_ms` 1 to
3 600 000) with its alternative in `$defs/item`. `$defs/body_usage` gains the optional `model` (model ID),
`access_origin` (`own`, `operator`, `platform`: whose model access the request used; AUD-04 W10) and `at` (time of the request). An older hub refuses the new kind and the new fields
(`request_invalid`), so the runner sends them only to a hub whose device state carries `billing.api`
(12-billing.md section 4).

## `credit_exhausted`

New session failure reason (`enums/session-failure-reasons.json`, `common.json#/$defs/session_failure_reason`), task
scheduling reason (`enums/task-states.json` `scheduling_reasons`) and operation error code
(`enums/operation-error-codes.json`, `common.json#/$defs/operation_error_code`), each placed after
`budget_exceeded`: an organization's prepaid credit is used up (12-billing.md section 7). The web error catalog has
texts for it.

## Audit action `credit.changed` (new)

`enums/audit-actions.json` (group `credit`) and `audit/credit.changed.json`: `{kind: start|grant|set|manual,
amount_micro_usd, balance_micro_usd, actor: operator|database|hub}` (signed integers for the amounts). The copies
`agent_hub.audit.actions` and `agent_hub.audit.detail_schemas` follow.

## Endpoints (new, 03-api.md section 4.11 → 12-billing.md section 6)

Rows in `api/INDEX.json` (owner `KOR-31`) and `tests/platform/contracts/spec/endpoints-kor31.json`, schemas
`api/<name>.{request,response}.json`, read actions `read.<read_resource>` with empty audit details:

| Endpoint | Name | Right | Read resource |
|---|---|---|---|
| `GET /v1/orgs/{org}/billing` | `org-billing` | org:member | `org.billing` |
| `GET /v1/workgroups/{wg}/charges` | `workgroup-charges` | wg:read | `workgroup.charges` |
| `GET /v1/tasks/{task}/charges` | `task-charges` | task:read | `task.charges` |

`org-billing` returns `{currency: "USD", balance_micro_usd (signed), exhausted, billing_active, price_list:
{version, valid_from, body} | null, credits (≤ 100), days (≤ 31, `day` = `YYYY-MM-DD`), workgroups}`; the price list
document is `api/objects.json#/$defs/price_list_body` (the keys of 12-billing.md section 2, closed; prices are numbers
or decimal texts as `agent_hub.billing.prices` accepts them). The charge endpoints return `{tasks (≤ 200) |
sessions, total_micro_usd}` with `tokens_micro_usd`, `runtime_micro_usd`, `tokens`, `runtime_ms` per row.

## `GET /v1/device/state`: `billing`

The active alternative gains `billing: {api: 1, active, orgs: {<org_id>: {exhausted}}}`. A hub of this release
always sends it; the schema keeps it optional so that the answers of older hubs stay valid (the runner treats its
absence as "billing API unknown").

## `audit/outbox.rejected.json`

`item_kind` accepts `runtime` (a rejected `runtime` item is audited like every other kind).

## Audit actions `billing.fx_set`, `billing.export` (Nachtrag 4: CHF equivalents for the books)

The operator sets the USD/CHF rate of a day (`billing.fx_set`: day, rate × 10^8, source, kind `first` or `correction`) and exports the
bookings of a period (`billing.export`: from, to, rows). Detail schemas in `audit/`, fixtures in `fixtures/audit/`.
USD stays the only currency of every API; CHF exists only in the hub's books and the operator's export.
