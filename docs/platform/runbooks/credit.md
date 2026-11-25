# Credit and prices: rolling out billing, setting credit, checking it works

Usage of DFX resources is charged from a prepaid credit per organization ([ADR-062](../adr/062-prepaid-credit-and-usage-billing.md),
[12-billing.md](../spec/12-billing.md)). There is no payment method and no top-up in the interface: the operator sets
the credit. Everything below runs on the **hub device** with the hub's environment (the same variables as the hub
service) unless a step says runner device.

## 1. Rollout without stopping anyone

The release changes nothing until a price list is in force. Order:

1. **Hub** of this release (migration `0014_billing` adds tables only; `agent-hub migrate`, then restart). From now on
   the device state carries `billing.api = 1, active = false`; nothing is charged, nothing is blocked.
2. **Runners** of this release on every platform device (they report the access origin and measure VM runtime only
   towards a billing hub, and pause sessions only while a price list is in force). A runner of an older release keeps
   working but does not stop sessions when credit runs out: update the platform devices before step 4.
3. **USD/CHF rate first** (every booking keeps its CHF equivalent for the books; without a rate the price list and
   credits are refused): `agent-hub billing fx set --day <today> --rate <CHF per USD> --source BAZG`, and from then
   on the daily job of section 5.
4. **Credit for the existing organizations**: there is no automatic start credit
   ([E-14](../KONZEPT.md#e-14)); `AGENT_HUB_BILLING_START_CREDIT_USD` stays unset or `0`. Before the price list of
   step 5 comes into force, the operator distributes credit by hand to every existing organization that should keep
   working on DFX resources (section 2: directly in the database or with `agent-hub billing credit`);
   otherwise every organization is exhausted at once. New organizations start with 0 until the operator credits them.
5. **Price list**: `agent-hub billing price-list add --file docs/platform/billing/price-list-2026-11-24.json`. The
   command refuses a list that misses a model of the hub's model catalog and names every organization that would have
   no credit; it refuses then unless `--confirm` is given. `--valid-from 2026-11-24T00:00:00Z` (never in the past)
   schedules it; without it the document's `valid_from` (or now) applies.
6. Within 5 s the settle job creates the accounts; runners see `billing.active = true` at their next device state.

Change prices later the same way with a new file (a new version; earlier charges keep their version).

## 2. Setting credit

Credit is distributed only by the operator, directly on the hub device (remote shell); there is no public endpoint
and no view for it ([E-14](../KONZEPT.md#e-14)). The way the operator uses is the direct one in the database (as the
hub's owner or writer role, `psql` on the hub database) – one statement sets the balance of one organization to an
amount in **micro-USD** (USD × 1 000 000):

```sql
INSERT INTO hub.credit_account (org_id, balance_micro_usd) VALUES ('<org-id>', 50 * 1000000)
  ON CONFLICT (org_id) DO UPDATE SET balance_micro_usd = EXCLUDED.balance_micro_usd;
```

To add instead of set: `UPDATE hub.credit_account SET balance_micro_usd = balance_micro_usd + 50 * 1000000 WHERE
org_id = '<org-id>';` (reading a value and writing it back would lose the charges booked in between).

The same works with the operator command on the hub device, which also stores the reason in the history (audit row
`credit.changed`):

```sh
agent-hub billing credit --org <org-id> --add 50 --reason "credit for November"
agent-hub billing credit --org <org-id> --set 10 --reason "correction"
agent-hub billing credit --all-orgs --add 5 --reason "goodwill"
agent-hub billing show [--org <org-id>]          # balance, exhausted, account columns (JSON lines)
```

What happens: only `balance_micro_usd` changes. Within 5 s the hub's settle job sees that it differs from
`booked_micro_usd`, records the difference in the history (`manual`, actor `database`), writes the audit row and aligns
the columns. Charges arriving at the same time are taken from the new value; nothing is lost or counted twice.

## 3. What users see when the credit is used up

- New tasks on DFX devices wait with the reason "credit used up" and start by themselves once credit is added; own
  devices of the organization keep working.
- Running sessions on DFX devices are paused (the running turn is interrupted, the session suspended with its full
  context); subagent sessions are stopped. After a credit the interrupted sessions continue with the platform prompt
  "Credit is available again …"; idle sessions continue with the next prompt.
- The header shows "Credit used up"; the settings section "Credit and usage" explains it. The user's own agent reads
  the same through `GET /v1/orgs/{org}/billing` (agent access, ADR-056).
- The overdraft is limited to what runs between the balance reaching 0 and the pause (settle ≤ 5 s, then the hint to
  the runner); a balance can therefore end slightly below 0.

## 4. Operations check (after the rollout)

1. `agent-hub billing price-list show` → `in_force` is the expected version.
2. `agent-hub billing show --org <test-org>` → account exists, balance = the credit distributed in
   step 4 of section 1, `exhausted: false`.
3. Run one short task in the test organization on a DFX device. Within a minute:
   `psql … -c "SELECT kind, amount_micro_usd, price_version, quantity FROM hub.charge ORDER BY at DESC LIMIT 5"`
   shows `runtime` rows (and `tokens` rows with `"access": "platform"` or `"operator"` when DFX model access was used);
   `agent-hub billing show --org <test-org>` shows the balance reduced by their sum.
4. `agent-hub billing credit --org <test-org> --set 0 --reason "check"` → within seconds the session is suspended with
   `credit_exhausted` (task view), a new task waits with "credit used up".
5. `agent-hub billing credit --org <test-org> --set <previous> --reason "check done"` → the waiting task starts, the
   interrupted session continues.

## 5. Daily USD/CHF rate and the export for the books

All prices, balances and screens are in USD only. For the books every booking also stores its CHF equivalent at the
rate of its day (Europe/Zurich). Source: the daily rates of the Federal Office for Customs and Border Security (BAZG),
which the Federal Tax Administration names for VAT – an XML file per business day, published at about 07:00 and valid
from the next day (Friday's file also covers the weekend); USD is quoted as CHF per 1 USD.

The daily job (on a machine with internet access; it runs the hub command on the hub device) sets the rate **valid on**
the day, i.e. from the file published the business day before:

```sh
D=$(TZ=Europe/Zurich date +%F)   # the day to set (Europe/Zurich)
P=$(python3 -I -c 'import sys,datetime as t;print((t.date.fromisoformat(sys.argv[1])-t.timedelta(1)).strftime("%Y%m%d"))' "$D")
R=$(curl -fsS "https://www.backend-rates.bazg.admin.ch/api/xmldaily?d=$P&locale=de" | python3 -I -c 'import sys,xml.etree.ElementTree as E;d=sys.argv[1];n="{https://www.backend-rates.bazg.admin.ch/xmldaily}";r=E.parse(sys.stdin).getroot();assert ".".join(reversed(d.split("-"))) in r.findtext(n+"gueltigkeit");u=r.find(n+"devise[@code=\"usd\"]");assert u.findtext(n+"waehrung").strip()=="1 USD";print(u.findtext(n+"kurs").strip())' "$D")
agent-hub billing fx set --day "$D" --rate "$R" --source BAZG
```

A day that already has a rate is refused without `--reason` (a correction; bookings made keep their rate). If the job
misses days, bookings use the newest rate before them and record its day; the hub logs `billing.fx stale` once an hour
after 4 days, and `agent-hub billing fx show` exits 1 (use it in monitoring). `agent-hub billing fx list --from D --to D`
shows the rates with every correction.

Export for the accountant (audited `billing.export`; no task content):

```sh
agent-hub billing export --from 2026-11-01 --to 2026-11-30 --format csv > bookings-2026-11.csv
agent-hub billing export --from 2026-11-01 --to 2026-11-30 --org <org-id>       # JSON lines, one organization
```

Columns: `at, org_id, kind, amount_micro_usd, chf_per_usd, fx_day, amount_micro_chf, reference` (micro = 10^-6). The
CHF total of a period is the sum of `amount_micro_chf` (each booking rounded once, half-even).

## 6. Rollback

Take a price list out of force by adding a list with zero prices is **not** the way (it still marks organizations
without credit as exhausted). To stop billing completely: stop the hub, remove the versions in force and restart —
the tables are append-only, so this needs the owner role:
`ALTER TABLE hub.price_list DISABLE TRIGGER price_list_no_change; DELETE FROM hub.price_list; ALTER TABLE hub.price_list
ENABLE ALWAYS TRIGGER price_list_no_change;`. Charges and history stay. Rolling the hub back below this release is not
supported once runners pushed `runtime` items (an older hub refuses the item kind and `agent-hub migrate` refuses a
newer schema).
