# ADR-091: Every subscription at a glance – home card and page "My subscriptions"

Status: Accepted (2026-11-25)

Implements [E-30](../KONZEPT.md#e-30) on the readings of [ADR-090](090-every-subscription-logged-in-centrally.md)
(KOR-69, [E-31](../KONZEPT.md#e-31)) and of the operator's token files
([ADR-081](081-model-access-fallback-and-dfx-access-on-own-hardware.md) §6); parity with the agent tool
([E-04](../KONZEPT.md#e-04)) is `agent-platform subscriptions list` of ADR-090 §4.

## Context

The owner of several Claude subscriptions wants their remaining quota on the home page of agent.dfx.swiss, in the
form of the page "Subscriptions" of the local dashboard: one line per provider with the time to the reset, a bar and
the used share; opened, the 5-hour window, the week, the week per model and every account with its own bar and the age
of its measurement; below a table with every column; "detailed" and "sort by used". Everything is measured centrally
(ADR-090 §4); nothing runs on the user's computer. The subscriptions that platform devices still read from the
operator's token files (`abo4`, `abo5`) are not records and appear only in `GET /v1/orgs/{org}/quotas`.

## Decision

1. **No new hub data.** The web client reads `GET /v1/me/subscriptions` (records with their newest reading) and opens
   every record itself (`GET /v1/subscriptions/{id}`, checked like every own record) for name, note, address, token or
   none ("not logged in") and use – the hub never sees them. A record the browser cannot open keeps its reading and
   shows "cannot be opened (code)". For the holder of the platform role `operator` in the verified platform log only,
   the token-file subscriptions are read from `GET /v1/orgs/{org}/quotas` of the organizations the log grants them, once
   per label (the newest read wins); the hub gives only their fullest window, shown as such. Records and token files
   have different keys (record ID, `operator:<label>`), so nothing is listed twice; a subscription that moved from its
   file to a record is listed once the operator deletes the file (ADR-090 §5). No display names for token files: they
   would need a new table, and the files are being replaced by records.
2. **Providers are drivers.** Rows are grouped by provider; a provider is one entry of a list (display name per ID,
   `claude` and `openai` today) and an unknown ID shows itself, so a further provider needs no change of the view.
3. **Figures.** Percentages are the used share as measured; bars are coloured by what is left – amber from 70 % used
   (30 % left), red from 90 % used (10 % left). The headline of a provider is the week of its most-used account (else
   its fullest window); the detail windows show the highest use of each window across the accounts. Nothing missing is
   shown as 0 %. A measurement older than 60 minutes is marked old. Times to the reset read as the local dashboard's
   (`5 d 19 h`, `3 h 12 min`, `4 min`, `<1 min`, due); ages as `N s/min/h/d ago`.
4. **Where.** A card on the home page (the situation view of the landing organization, which needs no choice since
   KOR-68 stage 0), collapsed and opened per provider, with a link to the page `/subscriptions` (both switches, the
   table) and from the settings. The card and the page load lazily; the first load of the app does not grow. Without
   any subscription the card and the page say so and lead to "Add subscription"; a subscription that is not logged in
   offers "Log in now". These two are where the login dialogs of ADR-090 hang in; until then they name the agent
   tool's command.
5. **Only the owner.** The data are the caller's own records; the token files only for the operator. Organizations,
   admins and other members see nothing of it here.

## Consequences

- The reported list of the first draft of KOR-68 (values sent from the user's computer) is not built.
- The view refreshes every minute; the devices measure every 15 minutes (05-runner §10.6).
- The browser reads the organizations' logs it has not read yet once before it opens records written by a device
  (their keys come from the verified logs).
