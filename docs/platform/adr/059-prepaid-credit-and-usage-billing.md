# ADR-059: Usage billing with prepaid credit (ideas 45–47)

Status: Accepted (2026-11-23)

## Context
- The complete concept (ideas 45–47, section 16) makes DFX model access and DFX execution time chargeable: tokens per
  model request, VM runtime to the second, also for subagents and temporary work VMs, paid only from prepaid credit.
  Fully own resources stay free and usable without credit. Running paid sessions are stopped in a controlled way when
  the credit is used up, stay resumable and continue after a credit.
- The owner decided on 2026-11-23: no payment method and no top-up in the interface; the operator sets the credit by
  hand (also directly in the database); prices are a fair market average chosen by the platform team; "we simply
  start" – the release must not stop any organization.
- What exists: the runner reports one `usage` outbox item per model request (tokens, `cost_micro_usd` by cost basis:
  OpenRouter's `usage.cost` for `platform`, list price for `api_key`, list price as an equivalent for
  `subscription`); the hub adds them to `usage_counter` (per session) and, for platform work, to `usage_daily`; the
  optional `PLATFORM_BUDGET_USD_PER_ORG_DAY` caps the daily OpenRouter spend of an organization (hub check H4 for
  `subagent.request`, runner refusal of new platform sessions, gateway 429; ADR-031, ADR-037, ADR-026). The web shows
  the per-session counters as the usage bar. Operator subscriptions (ADR-052) are reported as `subscription`, so the
  hub cannot tell them from a user's own subscription. Nothing measures VM runtime.

## Decision

### 1. What is charged (the four cases)
A **charge** is booked for exactly two things, each only when DFX provides it:
- **Tokens** of a model request that used **DFX model access**: the platform's OpenRouter key (`model_access =
  platform`) or an operator subscription (ADR-052). A user's own subscription or API key is never charged.
- **VM runtime** of a VM on a **platform device** (`device.kind = platform`, DFX hardware): the session VM of the
  workgroup and temporary work, test and browser VMs. An org device (own hardware) is never charged for runtime.

So own hardware + own access: nothing; DFX hardware + own access: runtime; own hardware + DFX access: tokens; both:
both. The rule is evaluated per booking, not per organization. **The hub decides**, not the reporting runner: tokens
are charged only for items of a platform device (today DFX model access exists only there: the platform key and the
operator subscriptions are refused on org devices, ADR-010, ADR-052), so the case "own hardware + DFX access" is
defined by the rule but cannot occur yet; it is charged by the same rule once DFX access reaches org devices. An org
device can therefore neither cause nor avoid a charge.

### 2. Prices are configuration
- The **price list** is a JSON document (`docs/platform/spec/12-billing.md` §2) with `currency`, `valid_from`, token
  prices per million tokens **as purchase prices** per model plus a `default`, one **factor per DFX access kind**
  (`platform`, `operator`: price = purchase price × factor, so a provider's price change is one edited
  number) and runtime prices per hour per VM size (`session`, `standard`, `large`, `xlarge`, `default`).
- Versions are rows of the append-only table `hub.price_list` (version, `valid_from`, body). The operator adds one with
  `agent-hub billing price-list add --file F [--valid-from T]`; `valid_from` must not lie in the past (a change only
  prices future consumption). A booking uses the version in force at the **measurement time** (clamped to the hub's
  receive time) and stores its number (`price_version`), so every charge can be recomputed.
- Currency: **USD**, by the owner's decision 18 ("alles in agent immer in usd zu bepreisen"): prices, bookings,
  balances, limits, the interface, the APIs and the operator commands use USD only; no second currency and no
  conversion is shown anywhere to users. Amounts are integers in micro-USD (`bigint`); each booking is rounded once,
  half-even, to a micro-USD.
- **No price list in force means nothing is charged and nothing is blocked** (the release starts this way).

### 3. Measuring
- **Tokens**: the runner's `usage` item gains three optional fields: `model` (the record's model), `access_origin`
  (AUD-04 W10: `own` - the user's own subscription or API key, free; `operator` - an operator subscription of DFX,
  ADR-052; `platform` - the platform's OpenRouter key; the hub reads it only from platform devices) and the measurement
  time `at`. Today the operator subscriptions report themselves to the hub as `subscription` like an own one; without
  the marker nothing they use would be charged. A hub that receives an item without the marker (an older runner) takes
  the session's `model_id`, its receive time and treats `model_access = platform` (or the provider cost basis) as
  `platform`, anything else as `own` - the operator case is then charged once the runner is updated.
- **Runtime**: a new outbox item `runtime` from platform-device runners: `{vm_id, vm_kind (session|work), size,
  session_id, from, to, runtime_ms}`. The runner's meter (table `runner.billing_meter`, one row per VM with the time
  measured through) runs every 5 s (a session that starts or stops between two ticks counts from or until the tick); it splits each interval of a **running** VM
  evenly (largest remainder, so the shares add up to the interval) between the sessions that hold it in that interval (states `agent_starting` … `idle`, `resuming`, `stopping`;
  never `suspended`); for a temporary work VM the requesting session gets all of it. A VM no session holds (warm pool,
  an idle VM before the idle stop) is not charged. Item and meter row are written in one transaction, so a crash
  never measures an interval twice and never loses one; a gap after a daemon restart is charged only while the VM
  still runs (otherwise the end is unknown and the gap is free) and is sent in pieces of at most one hour. Measuring
  starts when the runner first sees billing active for the org (never earlier), so a release does not charge the past. Org devices send no `runtime` items and the hub
  ignores one from them (logged, no charge). Other areas add VM sources by the runner extension point
  `<area>/billing_vms.py:VMS` (the temporary work VMs of KOR-29; an addition to the table of ADR-027 §4). The meter
  rows are dropped while the hub does not bill, so switching billing on again never charges the time in between.
- **Exactly once**: both items go through the outbox (gapless `origin_seq`, receipts, ADR-030): the charge is inserted
  in the handler's transaction, a replay runs no handler. Each charge also carries the item's `(device_id,
  origin_seq)` as a unique key as a second line of defence. A `runtime` item is accepted for a session of an org the
  device serves (also after the session moved to another device), so late items of a moved session are not lost.
- **Version**: an item is priced by the version in force at its measurement time (`at`, or `to` of a runtime interval;
  clamped to the receive time). An interval that spans a `valid_from` is priced by the version at its end (intervals
  are at most 5 s apart). Items of an older runner carry no time: their backlog is priced at the receive time. A
  reported time is never earlier than the session's creation (a backdated time cannot pick an older or no version).

### 4. Bookings and attribution
- `hub.charge`: one row per booking (org, workgroup, task, session, device, kind `tokens`|`runtime`, quantities,
  `amount_micro_usd`, `price_version`, measured `at`). Sums per task, session, workgroup and organization are read from
  it (indexes) and from `hub.charge_daily(org_id, day, wg_id, device_id, kind)` (one row per device like ADR-037, so
  pushes of different devices never lock the same row) for the dashboard history. Subagents are sessions of the task
  and are attributed like any session.
- The organization is the **cost bearer**: workgroups and tasks belong to exactly one organization, credentials and
  devices are assigned per organization, and the person who pays is the organization's operator contact.

### 5. Credit
- `hub.credit_account(org_id, balance_micro_usd, booked_micro_usd, updated_at)`. A push never locks it: each charge
  also inserts a row of `hub.charge_pending(charge_id, org_id, amount)`, and the job `billing.settle` (every 5 s, one
  short transaction per org) deletes the org's pending rows and subtracts their sum from both columns in one `UPDATE …
  SET x = x - sum` (no hot row, no lock-order deadlock between devices, no lost update). The **balance** of an org is
  `balance_micro_usd − Σ pending`. The history is the append-only `hub.credit_entry` (kind `start`, `grant`, `set`,
  `manual`); consumption is read from `hub.charge`.
- **Operator command** (audited `credit.changed`, the reason in the history): `agent-hub billing credit (--org ID |
  --all-orgs) (--add USD | --set USD) --reason TEXT` (settles first, then moves both columns),
  `agent-hub billing show [--org ID]`.
- **Direct path in the database** (the owner's wish): one statement
  `INSERT INTO hub.credit_account (org_id, balance_micro_usd) VALUES ('<org>', <micro-USD>) ON CONFLICT (org_id) DO
  UPDATE SET balance_micro_usd = EXCLUDED.balance_micro_usd;` sets the balance. Because only `balance_micro_usd`
  changes, the hub's job `billing.settle` (every 5 s and at start) locks the row, sees `balance ≠ booked`, writes a
  `manual` history entry with the difference, the audit row `credit.changed` (actor `database`) and sets `booked =
  balance`, all in one transaction. Concurrent bookings keep working (the settle moves both columns together), so a
  hand-set value is never lost and never corrupts the books. Adding instead of setting is `UPDATE hub.credit_account
  SET balance_micro_usd = balance_micro_usd + <micro-USD> WHERE org_id = '<org>';` (reading and then writing a value
  would lose the bookings in between).
- **Start credit**: setting `AGENT_HUB_BILLING_START_CREDIT_USD` (default 0). Accounts exist only while billing is
  active: every organization without one – existing ones at the first settle after the first price list came into
  force, new ones at their first booking or settle – gets an account with the start credit **as set at that moment**
  (history `start`); until then an organization counts with the configured start credit. `price-list add` names the
  organizations that would be exhausted when the list comes into force and refuses without `--confirm`, so a release
  with start credit 0 cannot stop everyone by accident.
- **Exhausted** means a price list is in force and the balance is ≤ 0. A booking may take the balance below 0 (a
  request in flight is booked in full); the overdraft is bounded by the stop rules below.

### 6. Enforcement
- **No new paid start**: the scheduler drops platform devices from the candidates of an exhausted organization; with
  an org device left the session runs there (own hardware stays usable), otherwise the task waits `scheduling` with
  the reason `credit_exhausted` (text in the web) and is placed automatically when credit arrives (signal
  `credit.changed` → re-scheduling of the org, like `log.appended`). The budget hook H4 (`subagent.request`) answers
  `credit_exhausted` for a session on a platform device (DFX access exists only there).
- **Controlled stop**: `GET /v1/device/state` gains `billing: {orgs: {<org>: {exhausted}}}` for the assigned
  organizations, plus `api: 1` and `active` (always present on a hub of this release: the runner sends `runtime` items
  and the new `usage` fields only to such a hub). When an org becomes exhausted or not exhausted (settle, operator,
  a price list coming into force) the hub sends the content-free hint `reconcile {}` to the org's devices, so runners
  react within seconds, not at the next 30 s fetch. The runner stops every chargeable session of an exhausted
  organization (all sessions of a platform device): a working main session is interrupted, then suspended; the
  transition `suspended` carries `credit_exhausted`; a subagent session is stopped (`stopping`/`stopped` with
  `credit_exhausted`; its operation fails and the parent learns it as for any ended subagent). No prompt is handed to a
  session of an exhausted org on a platform device and no suspended one is resumed (a follow-up prompt stays pending,
  never refused); a session the hub placed in a race is paused right after its start by the same rule. The pause is
  taken under the delivery lock of the session; a `suspend` the guest refuses while the interrupted turn ends is asked
  again. The session keeps its transcript and is resumable as after
  an idle suspension.
- **Continue after a credit**: when the organization is no longer exhausted the runner resumes the sessions it
  suspended for credit that were interrupted in a turn and delivers the fixed platform prompt "Credit is available
  again. Continue the interrupted work." (no user text; recorded in the runner table `runner.credit_pause`, delivered
  once with a stable prompt ID; the chat shows the session's `resuming` transition and the answer).
  Sessions that were idle stay suspended until the next prompt, as after an idle suspension.
- **Overdraft**: between the crossing of 0 and the stop lie one settle (≤ 5 s), the hint and the interrupt; the
  overdraft is bounded by that time. A runner of an older release does not stop sessions: runners are updated before
  the first price list is added (runbook).
- **Own resources**: a session on an org device with own access is never stopped, refused or charged.

### 7. Visibility
- `GET /v1/orgs/{org_id}/billing` (org members, read log): balance, exhausted, currency, price version in force with
  its prices, credit history, consumption and quantities per day (30 days) and per workgroup; `GET
  /v1/workgroups/{wg_id}/charges` (workgroup readers): sums and quantities per task (30 days); `GET
  /v1/tasks/{task_id}/charges` (workgroup readers): sums and quantities per session and kind. The web shows the balance in the header (warning when exhausted), the section "Credit and usage"
  in the organization settings and the charges in the task view. Nothing in the interface tops up credit.

### 8. The former "budget"
`PLATFORM_BUDGET_USD_PER_ORG_DAY` stays as an optional **daily safety cap of the operator** on the OpenRouter spend
(purchase cost, `usage_daily`), independent of credit; it is not a price and not the customer's balance. `usage_counter`
keeps the cost information of every session (provider cost, list price equivalents) for the usage bar; the customer's
charges are `hub.charge`.

### 9. CHF equivalent of every booking for the books (owner's decision 19)
- The owner's accounting must keep a CHF equivalent of every booking; one central USD/CHF rate per day suffices.
  USD stays the leading currency (section 2); CHF exists only in the hub's books and the operator's export.
- `hub.fx_rate` [AO]: rate per calendar day (CHF per 1 USD, exact decimal with at most 8 places, stored × 10^8),
  source, set time, actor `operator`, reason. Set only by the operator command `agent-hub billing fx set` (audited
  `billing.fx_set`); the hub asks no outside service. A further rate of a day that has one is a **correction**: it
  needs a reason and applies to bookings made after it; bookings already made are never recomputed (they keep the
  rate they were booked with - the books must not change after the fact; a correction of the past is a new
  accounting entry outside the platform, if the accountant wants one).
- Every charge and every credit history entry (start, grant, set, manual) stores, once and immutably: the amount in
  micro-USD, `fx_n` (the rate row used), `fx_day`, `chf_per_usd_e8` and `amount_micro_chf` = round_half_even(USD ×
  rate) in micro-CHF. The day of a booking is its calendar day in **Europe/Zurich** (computed by PostgreSQL); the
  rate is the newest row of the latest day at or before it, so a weekend, a holiday or a failed job takes the day
  before and the booking shows which day it used. Billing never stops for a missing rate; the settle job logs a
  warning once an hour when the newest rate is older than 4 days, and `agent-hub billing fx show` exits 1 then (for a
  monitoring job). Without any rate `price-list add` and `billing credit` refuse (billing cannot be switched on).
- CHF sums are the sums of the stored equivalents (each booking rounded once). The export `agent-hub billing export
  --from D --to D [--org ID] [--format json|csv]` lists every booking of the period (Europe/Zurich days) with time,
  organization, kind, USD, rate, rate day, CHF and reference (charge with workgroup, task, session or credit entry);
  no content of tasks; audited `billing.export`.
- Source of the rate: the daily rates of the Federal Office for Customs and Border Security (BAZG), the rates the
  Federal Tax Administration names for VAT; the operator's daily job sets the rate valid on each day (runbook).

## Consequences
- Billing is metadata at the hub (amounts, token counts, seconds); no content, so zero knowledge is unchanged.
- Release order: hub first (accepts the old items, publishes `billing`), then runners, then the start credit and the
  first price list. The migration only adds tables; an older runner keeps working (tokens billed by the fallback,
  runtime not measured, sessions not stopped). Rolling the hub back below this release is not supported once runners
  of this release pushed `runtime` items (an older hub refuses the kind); `agent-hub migrate` already refuses to run an
  older hub on a newer schema.
- `hub.charge` keeps one row per model request and per runtime interval for the books; it has no retention (sums for
  the dashboard come from `charge_daily`).
- Token counts are measured by the guest gateway inside the VM; their trust rests on the guest isolation (ADR-008).
  A later step can reconcile them against the provider's own usage figures.
- A platform device is trusted for its measurements (it is DFX hardware); an org device cannot cause a runtime charge.

## Alternatives considered
Pricing at the runner (prices would need a runner release and the hub could not version them), OpenRouter's reported
cost × factor (not versionable, unknown for subscriptions), one ledger row per booking with a running balance (hot row
per org), a balance column the operator edits without a booked column (a hand edit would be invisible in the history),
charging a shared session VM in full to every session (charges the VM several times), stopping sessions in the hub
(only the runner can interrupt and suspend), a payment method (excluded by the owner).

## Deviation from DESIGN.md
None beyond ADR-002.
