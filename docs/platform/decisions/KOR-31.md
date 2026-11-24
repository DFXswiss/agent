# KOR-31 – decisions (usage billing with prepaid credit)

Architecture: [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md); specification: [12-billing.md](../spec/12-billing.md);
operations: [runbooks/credit.md](../runbooks/credit.md).

1. **Currency USD** (owner's decision 18): prices, balances, interface, APIs and commands in USD only; amounts in
   micro-USD integers, each booking rounded once half-even.
2. **Cost bearer = organization.** Workgroups, tasks, devices and credentials belong to one organization.
3. **Access origin from the runner, decided by the hub** (AUD-04 W10): `own`, `operator`, `platform` per `usage` item;
   the hub reads it only from platform devices. The case "own hardware + DFX model access" is in the rule but cannot
   occur today (DFX access exists only on platform devices).
4. **Prices as purchase price × factor per origin** (`platform` 1.06 = OpenRouter's credit fee, `operator` 1.0 =
   Anthropic list price), runtime per VM hour per size; a model without entry uses `default` and `price-list add`
   refuses a list without an entry for a DFX model of the catalog. First list: `docs/platform/billing/price-list-2026-11-24.json`
   (market research in the workspace report).
5. **A push never locks the balance** (design review H2): charges go to `charge_pending`, the job `billing.settle`
   (5 s) applies them per org. The balance shown is settled balance − pending sum, so it is exact at any moment.
6. **Hand-set balance** (owner's wish): `balance` vs. `booked` columns; the settle job records a difference as a
   `manual` entry with an audit row. Concurrent bookings are not lost (see the runbook).
7. **Start credit only while billing is active**, with the value configured at that moment; `price-list add` names
   organizations that would be exhausted and needs `--confirm` (design review H3). Proposed start credit: 20 USD.
8. **Shared workgroup VM**: runtime split evenly (largest remainder) between the sessions holding it; a VM nobody holds
   is not charged; a work VM's runtime goes to the requesting session (extension point `<area>/billing_vms.py:VMS`
   for KOR-29). Measuring starts when the runner first sees billing active; a gap of a stopped VM is free.
9. **Controlled stop = suspend** (AUD-04 W12): main sessions are interrupted and suspended with `credit_exhausted`;
   subagents (one turn) are stopped. Follow-ups wait while exhausted. After a credit the interrupted sessions get the
   fixed continuation prompt (runner-authored, stable prompt ID); it has no hub event, the chat shows the `resuming`
   transition and the answer.
10. **A start placed in a race** (credit ran out between placement and start) is not refused but paused right after
    it started by the same rule; the hub's scheduler keeps new sessions off platform devices of an exhausted org.
    No prompt is delivered to a session of an exhausted org on a platform device (also not to an idle one before its
    pause); the pause runs under the deliverer's session lock and asks `suspend` again while the interrupted turn ends.
    The operator-subscription origin is kept in `runner.operator_origin`, because the binding is pruned when a session
    ends and usage may be processed later.
11. **The former daily budget** `PLATFORM_BUDGET_USD_PER_ORG_DAY` is superseded by the balance (AUD-04 A5): it stays an
    optional operator cap on OpenRouter purchase cost, unset by default, and is no longer part of the customer view.
12. **Reads without a browser** (decision 10a): the three routes are browser routes and therefore open to the user's
    agent (ADR-056 §4); the operator reads with `agent-hub billing show`.
13. **Retention**: `hub.charge` keeps every booking (books); the dashboard reads the daily sums.
14. **Runtime precision**: the meter ticks every 5 s; a session that starts or stops between ticks counts from or until
    the tick (attribution at state changes would need a hook into the journal of every session area).
15. **Traceability**: `e2e/platform/traceability.json` and `01-requirements.md` gain R-P.14 / E2E-40 – the E2E pre-check
    refuses a spec whose tag is not in the matrix, and the matrix must mirror the requirements; earlier feature
    packages did the same for E2E-37 to E2E-39.
16. **What the hub sees** (AUD-04 A4): the new columns are mapped in `e2e/platform/checks/hub_columns.py` under the
    category `billing`; the operator's free text (price list document, the reason of a credit) is typed `operator`
    (written on the hub device by the operator, never by a user).
17. **Review findings kept**: the scheduler and the budget hook read the billing area directly (a discovered point
    would add indirection for one reader); the price list is parsed per booking (a small JSON, no measurable cost yet);
    subagents are stopped, not suspended (one turn, AUD-04 W12 concerns main sessions); the header reads the balance
    once a minute per tab (one read-log row each).
18. **CHF equivalent for the books** (owner's decision 19, ADR-062 §9): one USD/CHF rate per day in `hub.fx_rate`,
    set only by the operator command; corrections are further rows with a reason and never change bookings made
    (the books must not move after the fact). The rate is stored × 10^8 as an integer (the export format of the hub
    knows no `numeric`). Equivalents in micro-CHF, rounded once half-even per booking; CHF totals are sums of the
    booked equivalents. The booking day is computed in PostgreSQL (Europe/Zurich), because the hub image may lack
    the time zone data of the operating system. Without any rate the price list and credits are refused, so billing
    cannot start without one; a missing day takes the newest rate before it and never stops billing; stale after 4
    days (log warning hourly, `fx show` exits 1 - the hub has no metrics endpoint). Source: BAZG daily rates (the
    rates the Federal Tax Administration names for VAT), the rate valid on the day.
19. **Numbers on the united tree** (INF-06): ADR-062, hub migration `0014_billing`, runner migration
    `0013_billing`, spec `12-billing.md`, scenario E2E-46 (the next free numbers when the package was carried over).
