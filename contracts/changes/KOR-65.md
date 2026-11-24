# KOR-65 – contract changes

ADR-027 section 3. Additive only.

- `requests/github.connect.request.json`: optional `app` (`oauth_app`, `github_app`) - the device flow to run; without
  it the device's preferred one (the OAuth App when set up). `requests/objects.json` `failure_code`:
  `flow_unavailable` (the asked device flow is not set up on the device).
- `events/objects.json` `body_session_status`, waiting variant: `waiting_for` also `github_access` (the acting user's
  connection does not reach the repository) and optional `next` (`github_next_step`: `action` of `connect`,
  `install_app`, `approve_app`, `authorize_sso`, `new_token`, `check_access`; `owner`; optional `url`, https or the
  loopback host of the doubles).
- `api/me-devices.response.json`: optional `features` per device (what it announced on its live channel, e.g.
  `github.ways`, `github.device_flow`, `github.operator`).
