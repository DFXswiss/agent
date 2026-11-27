# ADR-091: Every subscription at a glance – home card and page "My subscriptions"

Status: Accepted (2026-11-25); §3 corrected 2026-11-26 (what is left, all windows per subscription)

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
operator's token files are not records and appear only in `GET /v1/orgs/{org}/quotas`.

## Decision

1. **No new hub data.** The web client reads `GET /v1/me/subscriptions` (records with their newest reading) and opens
   every record itself (`GET /v1/subscriptions/{id}`, checked like every own record) for name, note, address, token or
   none ("not logged in") and use – the hub never sees them. A record the browser cannot open keeps its reading and
   shows "cannot be opened (code)". For the holder of the platform role `operator` in the verified platform log only,
   the token-file subscriptions are read from `GET /v1/orgs/{org}/quotas` of the organizations the log grants them, once
   per label (the newest read wins; an organization whose answer fails is left out); the hub gives only their fullest
   window, shown as such. A token file whose name equals the name of an own record (ignoring case) is that
   subscription, moving from its file to a record (ADR-090 §5): only the record's row is listed, with the file's
   reading when the record has none – nothing is listed twice. No display names for token files: they would need a
   new table, and the files are being replaced by records.
2. **Providers are drivers.** Rows are grouped by provider; a provider is one entry of a list (display name per ID,
   `claude` and `openai` today) and an unknown ID shows itself, so a further provider needs no change of the view.
3. **Figures: what is left, by the existing parts.** Nothing is computed a second time; the page and its card put the
   existing parts together:
   - **What is left** (`freePercent` of `web/src/core/credentials/quotas.ts`, shared by the page, the card and the
     settings sections "My own subscription" and "Model access and quotas"): E-30 asks for the remaining quota, so
     every figure – shares, bars, the table – is what is LEFT of a window ("82% free"): 100 % less the used share, never
     below 0 %; a window whose reset has passed is free again (as the pool's load of the local dashboard counts it).
     Bars are filled as far as something is left and coloured by it – amber at 30 % left or less, red at 10 % or less.
     The windows are ordered and named as `quotas.ts` reads them (5 hours, week, week per model).
   - **State of a subscription** – usable or exhausted, its binding window, its release – is the hub's, by the one rule
     of ADR-081 §6 (`agent_hub.models.quotas`: `binding_window`, `subscription_state`), extended from the own and the
     operator's subscriptions to every subscription record: `GET /v1/me/subscriptions` names it per record (`state`);
     a token file brings its state and `until` from `GET /v1/orgs/{org}/quotas`. The binding window is the window of
     the whole subscription with the highest use (a window whose reset has passed counts 0; the week on a tie; a week
     per model binds only its model); a subscription is exhausted until the latest reset of its full windows (or by
     its latest observed limit); an unreadable one counts as usable, as in that summary.
   - **Provider line.** The total of all its accounts, never its fullest account: each window (5 hours, week, week per
     model, fullest window) counts the accounts that have a reading of it – what is left is the mean of their shares
     left, every account weighted equally (an account without that window does not count), its time the one to the
     next reset among them; these are the detail windows. The headline ("free") is the mean of what each subscription
     has left in its binding window – the smaller rest of its 5-hour window and its week (2026-11-27: "wenn ein abo im
     5h limit aufgebraucht ist, dann musst du es in der gesamtberechnung mit 0% frei kalkulieren") –, every
     subscription weighted equally; a subscription whose 5 hours or week are used up counts 0 %, unreadable ones are
     left out and named with their number; its time is the next reset among those binding windows. The line says
     that it stands for all accounts ("all 9", with one account "1 account"), so it is not taken for an account of the
     same name, how many accounts the hub counts as usable now ("usable now: 3 of 9") and, when one waits for a known
     one, the next release ("next release 14:05 (in 1 h 30 min)", the time written as the counter writes it), counted
     over the hub's states of the listed rows as the summary of ADR-081 §6 counts them – only the browser knows which
     rows are listed (names are sealed; a token file named like a record is that record).
   - **Every subscription** shows the source and time of its measurement and ALL its windows (5 hours, week, week per
     model; a token file only its binding one), each with share left, bar and time to its reset. The hub's binding
     window is marked ("binding"); an exhausted subscription is marked "not usable now until 14:05 (in 1 h 30 min)" –
     also when its week is nearly free.
   - **Table.** Per subscription the share left ("Free") and the reset of every window.
   - "least free first" orders the providers by what is left of their headline. Nothing missing is shown as 0 %. A
     measurement older than 60 minutes is marked old; the headline is old when one of the readings it counts is. Times
     to the reset read as the local dashboard's (`5 d 19 h`, `3 h 12 min`, `4 min`, `<1 min`, due); ages as
     `N s/min/h/d ago`; a release time as the time of day, with the date when it is not today.
   - History (2026-11-26): the headline was first the week of the most-used account and the windows the highest use
     across the accounts – with nine subscriptions it read 99 % while about a fifth of the weekly quota was left. Then
     the figures were the used share and each account showed one bar, its week, so an account with 5 hours at 100 % and
     the week at 13 % looked green and free while it could not be used. Both did not meet E-30 and were corrected.
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
