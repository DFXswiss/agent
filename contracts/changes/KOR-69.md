# KOR-69 – contract changes

ADR-027 section 3 (additive). Decision: ADR-090 (every Claude subscription logged in centrally).

## Credential body (`credentials/claude_subscription.json`)
- `token` becomes optional (a further subscription that is not logged in); new optional `name`, `note` and
  `use {pool, orgs}`; `$defs` `email`, `name`, `note`.

## API (`api/INDEX.json`, `api/objects.json`)
| Endpoint | Purpose |
|---|---|
| `GET /v1/me/subscriptions` | the caller's subscriptions with their newest reading |
| `GET/PUT/DELETE /v1/subscriptions/{id}` | one own subscription record; PUT for further ones only |
| `GET /v1/device/subscriptions?owner_id=` | metadata of an owner's subscription records for an eligible device |
| `GET/PUT /v1/device/subscriptions/{id}` | device read, and the write while answering `claude.login` or to re-seal |

New objects `subscription_meta`, `subscription_reading`, `subscription_row`; `GET /v1/device/state` adds
`subscriptions {api}`.

## Device request `claude.login` (`requests/`)
The root body adds `subscription_id`, `name`, `note` and `email`.

## Outbox
`quota` adds `subscription_id` (both forms).

## Audit
Actions `read.me.subscriptions`, `read.subscription`, `read.device.subscription`, `read.device.subscriptions`.
