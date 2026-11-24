# 11 Billing: usage charges and prepaid credit

Status: binding (KOR-31, ADR-062). Concept: ideas 45–47 and section 16. Amounts are integers in micro-USD unless a
field says USD.

## 1. Terms
- **DFX model access**: tokens through the platform's OpenRouter key (`model_access = platform`) or an operator
  subscription (ADR-052). **Own access**: the user's own `claude_subscription` or `anthropic_api_key`.
- **DFX hardware**: a device with `kind = platform`. **Own hardware**: `kind = org`.
- **Charge**: one booking of tokens or runtime, priced by one price list version.
- **Cost bearer**: the organization of the session.
- **Exhausted**: a price list is in force and the organization's balance is ≤ 0.

## 2. Price list
```json
{
  "currency": "USD",
  "valid_from": "2026-11-24T00:00:00Z",
  "tokens": {
    "factors": {"platform": 1.25, "operator": 0.5},
    "models": {"deepseek/deepseek-v4.1-flash-1017": {"input": 0.15, "output": 0.6, "cache_read": 0.003, "cache_write": 0.15}},
    "default": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
  },
  "runtime": {"per_hour": {"session": 0.2, "standard": 0.2, "large": 0.4, "xlarge": 0.8, "default": 0.8}},
  "note": "free text"
}
```
- `currency` must be `USD`. Token prices are **purchase prices in USD per million tokens** (≥ 0, at most 1000);
  `factors` (0 ≤ f ≤ 100) turn them into the customer price per DFX access kind. Thinking tokens are part of
  `output_tokens` and have no own price. A model without entry uses `default`.
- Runtime prices are USD per VM hour (≥ 0, at most 1000) per size; `session` is the workgroup VM, `standard`, `large`,
  `xlarge` the work VM sizes; an unknown size uses `default`.
- Unknown keys are refused (`price_list_invalid`), so a typo never silently prices 0.
- Token charge = round_half_even(Σ count_k × price_k × factor) micro-USD (USD per Mtok = micro-USD per token).
  Runtime charge = round_half_even(runtime_ms × per_hour / 3.6) micro-USD.
- Version in force at time t: the row of `hub.price_list` with the greatest `valid_from` ≤ t (ties: highest version).
- `price-list add` refuses a list without an entry for a model the hub's model catalog offers through DFX access
  (`access.platform.allowed`, `access.subscription.allowed`), so a DFX model is never priced by `default` by accident.

## 3. Data (hub, migration 0014)
| Table | Columns | Notes |
|---|---|---|
| `hub.price_list` [AO] | `version` int PK ≥ 1, `valid_from` timestamptz, `body` jsonb, `created_at` | append-only |
| `hub.credit_account` | `org_id` uuid PK, `balance_micro_usd` bigint, `booked_micro_usd` bigint default 0, `updated_at` timestamptz default now() | balance may be negative |
| `hub.credit_entry` [AO] | `id` bigserial PK, `org_id`, `at`, `kind` (`start`,`grant`,`set`,`manual`), `amount_micro_usd` (signed), `balance_micro_usd` (after), `reason` text NULL (≤ 500), `actor` text (`operator`,`database`,`hub`) | history of credits |
| `hub.charge` [AO] | `id` bigserial PK, `org_id`, `wg_id`, `task_id`, `session_id`, `device_id`, `origin_seq`, `kind` (`tokens`,`runtime`), `at`, `amount_micro_usd` ≥ 0, `price_version`, `quantity` jsonb (token counts + model + access, or runtime_ms + vm_kind + size + vm_id) | UNIQUE (`device_id`, `origin_seq`); indexes (`org_id`,`at`), (`task_id`), (`session_id`) |
| `hub.charge_pending` | `charge_id` bigint PK, `org_id`, `amount_micro_usd` | charges not yet settled into the balance |
| `hub.charge_daily` | PK (`org_id`,`day`,`wg_id`,`device_id`,`kind`), `amount_micro_usd`, `quantity` bigint (tokens or runtime ms) | one row per device (ADR-037) |

Balance of an org = `credit_account.balance_micro_usd − Σ charge_pending.amount_micro_usd` (the start credit while it
has no account).

## 4. Outbox items (runner → hub, `03-api.md` §6.1)
- `usage` (existing) gains optional `model` (model ID), `access_origin` (`own`, `operator`, `platform`; AUD-04 W10) and `at` (time of the request). Without
  them the hub uses the session's `model_id`, DFX access = `model_access = platform` or (`model_access` unknown and
  `cost_basis = provider`), and its receive time.
- `runtime` (new): `{session_id, vm_id, vm_kind: "session"|"work", size, from, to, runtime_ms}` with `0 < runtime_ms ≤
  to − from` (ms) and `runtime_ms ≤ 3 600 000`, `to` at most 300 s after the receive time. Scope: a session of an
  org the device serves (`403 forbidden` otherwise, `404 not_found` for an unknown session). Charged only when the
  pushing device is a platform device. The runner sends it only to a hub whose device state has `billing.api`.
- The runner sends the new `usage` fields only to a hub whose device state has `billing.api` (older hubs refuse
  unknown fields). The hub reads `access_origin` only from platform devices; `operator` and `platform` are charged with the factor of
  that name, `own` is free.

## 5. Device state (`GET /v1/device/state`)
`billing`: `{"api": 1, "active": bool, "orgs": {"<org_id>": {"exhausted": bool}}}`, always present on a hub of this
release; `active` = a price list is in force; `orgs` lists every organization with a workgroup assigned to the device.
When an org's `exhausted` changes the hub sends `reconcile {}` to the devices assigned to it.

## 6. Hub API
- `GET /v1/orgs/{org}/billing` (browser, `org:member`; read log `read.org.billing`):
  `{currency, balance_micro_usd, exhausted, billing_active, price_list: {version, valid_from, body} | null,
  credits: [{at, kind, amount_micro_usd, balance_micro_usd, reason, actor}] (newest 100), days: [{day,
  tokens_micro_usd, runtime_micro_usd, tokens, runtime_ms}] (last 30 UTC days), workgroups: [{wg_id,
  tokens_micro_usd, runtime_micro_usd, tokens, runtime_ms}] (last 30 days)}`.
- `GET /v1/workgroups/{wg_id}/charges` (browser, `wg:read`; read log `read.workgroup.charges`): `{tasks: [{task_id,
  tokens_micro_usd, runtime_micro_usd, tokens, runtime_ms}] (last 30 days, by total descending, at most 200),
  total_micro_usd}`.
- `GET /v1/tasks/{task_id}/charges` (browser, `wg:read` of the task's workgroup; read log `read.task.charges`):
  `{sessions: [{session_id, tokens_micro_usd, runtime_micro_usd, tokens, runtime_ms}], total_micro_usd}`.
- Amounts in these answers are charges; `tokens` = input + output + cache read + cache write of charged requests.
- Errors: `credit_exhausted` (task/session reason, operation code).

## 7. Enforcement
| Where | Rule |
|---|---|
| Scheduler (`03-api.md` §4.6.2) | exhausted org: platform devices are not candidates; no candidate left → `scheduling`/`credit_exhausted`; `credit.changed` re-schedules the org |
| H4 (`subagent.request`) | exhausted org and requesting session on a platform device → `denied`/`credit_exhausted` |
| Runner (platform device) | exhausted org: main sessions interrupted (if working) and suspended with `credit_exhausted`, subagent sessions stopped with `credit_exhausted`; no prompt is delivered and no resume happens (a start placed in a race is paused right after it); not exhausted again: interrupted sessions resumed with the fixed continuation prompt |

Chargeable session (runner view): every session of a platform device (runtime is charged). Org devices never stop
or refuse anything for credit.

## 8. Operator
- `agent-hub billing price-list add --file F [--valid-from ISO] [--confirm]` (valid_from ≥ now; default now; names
  the orgs that would be exhausted and refuses without `--confirm` while there are any) and `price-list show`.
- `agent-hub billing credit (--org ID | --all-orgs) (--add USD | --set USD) --reason TEXT` (audit `credit.changed`).
- `agent-hub billing show [--org ID]` (balances, exhausted, price version).
- Setting `AGENT_HUB_BILLING_START_CREDIT_USD` (decimal ≥ 0, default 0): balance of a new account.
- Direct database path: see ADR-062 §5 and the runbook `docs/platform/runbooks/credit.md`.
- Requests (ADR-079 §4, E-05): when a task's attention record enters `credit_empty`, every user with the platform role
  `operator` gets one `operator.request` (`ref` = `credit_exhausted:<org>`) per organization and occasion (not again
  before a later `credit.changed` of the organization; not while an earlier one of the same reason is unread);
  `agent-hub platform waiting` lists the organizations waiting for credit. A start credit above 0 together with the
  switches of ADR-079 multiplies per new organization: the owner's decision F1 bounds it first.

## 9. Web
- Header item: balance (`$12.34`), red with "Credit used up" when exhausted; hidden when billing is inactive.
- Settings section "Credit and usage" (USD only) (org): balance, exhausted notice ("Paid resources are paused until the operator
  adds credit. Own devices with own model access keep working."), price list in force (per Mtok after factor, per VM
  hour), history of credits, consumption per day and per workgroup. No top-up control.
- Task view: charges per session (tokens, runtime) and total.
- Reason text `credit_exhausted` for tasks waiting and sessions suspended; the person sees that credit has been
  requested from the operator (ADR-079).

## 10. Books: CHF equivalent (owner's decisions 18 and 19, ADR-062 §9)
- `hub.fx_rate` [AO]: `n` PK, `day`, `chf_per_usd_e8` (CHF per USD × 10^8, > 0), `source`, `set_at`, `actor`
  (`operator`), `reason` (required for a further rate of the same day).
- `hub.charge` and `hub.credit_entry` gain `fx_n`, `fx_day`, `chf_per_usd_e8`, `amount_micro_chf` (all set or all
  null; null only for a booking made before any rate existed).
- Rate of a booking: newest row of the latest day ≤ the booking's day in Europe/Zurich. CHF = round_half_even(micro-USD
  × chf_per_usd_e8 / 10^8) micro-CHF, once per booking.
- Operator: `agent-hub billing fx set --day D --rate R --source S [--reason TEXT]`, `fx show` (exit 1 when missing or
  older than 4 days), `fx list [--from D] [--to D]`, `export --from D --to D [--org ID] [--format json|csv]` (fields
  `at, org_id, kind, amount_micro_usd, chf_per_usd, fx_day, amount_micro_chf, reference`).
- Users see USD only: no CHF in the web, in the user APIs or in the device state.
