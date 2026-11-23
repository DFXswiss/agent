# ATS-02 – contract changes

ADR-027 section 3 (additive). The attack suite ATK-36 (`07-testing.md` section 2.3: session-token brute force and
foreign tokens on MCP, hooks and gateway, "constant-time, rate-limited, journaled") found that the guest API neither
limited nor journaled failed token checks. The guest now binds a session token to the UID of the connecting process
(`05-runner.md` section 4.4: one token per session), counts failed checks per connecting UID and refuses further
failures for a while; it journals the attempts. The producer is the guest (agent `agent_guest`); the runner only logs
`guest.error` codes, the hub never sees them.

## `guest/journal/record.json`: `guest.error` codes `auth_failed`, `auth_rate_limited` (additive)

- `auth_failed`: a request to `/mcp`, `/hooks/<Event>` or `/model/…` carried no token, an unknown token, the token of
  an inactive session or the token of another session than the one of the connecting UID. The record carries the
  session ID of the connecting UID's session when there is one (else `session_id` is null, VM level). At most
  the limit per window is journaled per UID.
- `auth_rate_limited`: the connecting UID exceeded the failure limit; journaled once when the limit is reached.
  Further failures of that UID inside the window are answered without a record.

## `guest/error.response.json`: `rate_limited` (additive)

`429 {"error":"rate_limited"}` with `Retry-After` from `/mcp` and `/hooks/<Event>` while the limit holds (the model
gateway answers in the Anthropic error shape `rate_limit_error`).

Fixtures: new valid `guest/journal/guest.error/{auth-failed,auth-rate-limited}.json`,
`guest/error.response/rate-limited.json`.
