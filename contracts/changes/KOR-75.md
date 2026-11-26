# KOR-75 – contract changes

ADR-027 section 3 (additive). Decision: ADR-100 (who used how much of my subscriptions).

## API (`api/INDEX.json`)
| Endpoint | Purpose |
|---|---|
| `GET /v1/me/subscriptions/usage` | tokens, sessions and the estimated share of the quota per person and subscription for the running 5-hour window, the week or a chosen span |

New `api/subscriptions-usage.request.json` and `api/subscriptions-usage.response.json`. `GET /v1/device/state`:
`subscriptions.api` 2 announces `usage.subscription_id`.

## Outbox
`usage` adds `subscription_id` (UUID or null): the subscription record that served the requests.

## Audit
Action `read.me.subscriptions.usage`.
