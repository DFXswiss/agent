# KOR-71 – contract changes

ADR-027 section 3 (additive). Decision: ADR-091 (full login of further subscriptions, renewal by the measuring device).

## Credential body (`credentials/claude_subscription.json`)
- New kind `oauth_login`; new optional `refresh_token`, `scopes`, `subscription_type`, `rate_limit_tier` and
  `relogin` (`renewal_refused`). `expires_at` also for `oauth_login`.

## Device requests (`requests/objects.json`)
- The failure code `scope_missing` also answers a `claude.login` whose login lacks `user:profile` or
  `user:inference` (no new code).

## API
- No new field. `PUT /v1/device/subscriptions/{id}` without `request_id` is also accepted from the device that
  measures the owner (03-api.md section 6.1).
