# ADR-102: Who used how much of my subscriptions – tokens per person and subscription, an estimated share of the quota

Status: Accepted (2026-11-26)

Builds on [ADR-090](090-every-subscription-logged-in-centrally.md) (subscriptions as records, `use.orgs`, the
readings), [ADR-091](091-subscriptions-at-a-glance.md) (the page "My subscriptions") and
[ADR-083](083-who-speaks-in-a-shared-session.md) §2 (`triggered_by`, the consumption per person through DFX access).
Work package KOR-75. No new rule of the concept: the owner asked for an overview within
[E-30](../KONZEPT.md#e-30) and [E-31](../KONZEPT.md#e-31).

## Context

The owner of several Claude subscriptions switched them on for an organization (`use.orgs`, ADR-090 §3). The sessions
of colleagues, and of agent accounts, now draw on the same 5-hour and weekly quotas. The owner asked on 2026-11-26 for
an overview "where I see which of my colleagues used how many tokens, or how much of the subscription". The hub knew
the tokens per session (`hub.usage_counter`), the person behind a request (`triggered_by`, ADR-083 §2) and the newest
reading per subscription (`hub.subscription_quota`, the owner's `quota_snapshot`), but not which subscription served a
request: only the runner knew it (the pool binding `sub-<id>`, or the owner's own record).

The provider measures the quota per subscription only; nobody measures it per person.

## Decision

### 1. The runner names the subscription of a request, the hub keeps tokens per subscription and person

- The `usage` outbox item gains `subscription_id` (record ID or null), sent only to a hub whose device state has
  `subscriptions.api >= 2` (an older hub refuses unknown fields). The runner takes it from the session's access
  (`agent_runner.model.served`): a pool entry `sub-<id>` gives that record; a token file of the operator gives null (no
  record); a session once provisioned from the pool whose binding ended gives null (its late usage is not the owner's
  own); otherwise the owner's own subscription gives the ID of the cached primary record. Deltas of different
  subscriptions are never merged into one item.
- The hub (handler after the usage base handler) adds the item's requests and four token counts to
  `hub.subscription_usage` (migration 0042): one row per record, five-minute slot of the request time, person and
  session. The person is `triggered_by` when that person wrote a prompt of the session's task (the rule of the
  charges), else the session's owner. A row is kept only when the record may serve the session: the primary record of
  the session's owner, or a further record whose use mirror names the session's organization or the pool. A wrong or
  foreign record is dropped silently, so a device can neither refuse an item nor book it to someone else's
  subscription. The rows go with their record; no content, no token, no name.

### 2. The answer: windows, a chosen span, an estimated share

`GET /v1/me/subscriptions/usage?period=five_hour|week|range[&since&until]` (03-api.md §4.8):

- `five_hour` / `week`: the **running window of each subscription**, from its newest reading's `resets_at` minus 5 h /
  7 d until now; `measured` carries that window's `used_percent`, `resets_at` and the time of the reading. Without such
  a reading (none, unreadable, no reset, reset passed) the last 5 h / 7 d count and `measured` is null.
- `range`: a span the user chooses (at most 92 days); the provider measures no quota for it, so `measured` is null.
- Per person: requests, input, output, cache read and cache write tokens, their sum, the distinct sessions,
  `token_share` (the person's tokens over all tokens on that subscription in the span) and
  `estimated_percent` = `token_share` × `measured.used_percent`. It is labelled **estimated** everywhere: it splits the
  measured use – including use outside the platform and the provider's own weighting of token kinds – in proportion
  to the tokens the platform saw.
- Spans are exact to five minutes (the slot of the start counts whole).

### 3. Who sees what

- The **owner** sees every own subscription (also unused ones) with every person and the total.
- **Every person** sees their own use of other people's subscriptions: such a subscription is listed when the caller
  used it in the span, with the caller's row only and no total.
- Organizations and admins see nothing more here. Numbers only.

### 4. Where

- Web: the page "My subscriptions" gains the section "Who used how much": the period (5-hour window, week, choose) and
  per subscription a table person × tokens × sessions × share × estimated share, the measured value and its age, the
  total for own subscriptions. Below it, the usage through DFX access per person from the organization's billing
  (ADR-083 §2, the last 30 days) with a link to the settings section "Credit and usage" – reused, not rebuilt.
- Agent tool (E-04): `agent-platform subscriptions usage [--period five_hour|week|range] [--since] [--until]`, also
  reachable through the MCP server (`search`/`run`, KOR-63); own subscriptions carry the name and address as `list`
  shows them; `dfx_access` names `agent-platform billing --org`.

## Consequences

- The hub sees which subscription served how many tokens of which person in which five minutes – metadata of the same
  kind as the charges per person (04-crypto §7). It already saw the tokens per session, the person and which
  organizations may use a subscription.
- A runner without this release sends no `subscription_id`: its sessions are missing from the overview, nothing is
  wrong. Roll-out: hub first (it announces `subscriptions.api: 2`), then runners.
- Use before the roll-out is not attributed retroactively.
- The share of the quota stays an estimate; a better split needs per-person measurements the provider does not offer.

## Alternatives considered

- Per-person quota readings: the provider has none.
- Joining `hub.usage_counter` with the session's access at read time: a session may move between subscriptions
  (renewal, exhaustion, ADR-081), and the counter has no time; the share of a window could not be computed.
- Hourly slots: the 5-hour window starts at any minute; five minutes keeps the error small at low cost.

## Deviation from DESIGN.md

None.
