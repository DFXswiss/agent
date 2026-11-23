# RUN-13 acceptance evidence (Claude subscription login: setup-token relay and mailbox automation)

## Unit and integration tests (`make platform-ci`)
`tests/platform/runner/login/` (105 tests): real PostgreSQL store, the RUN-09 request frame and hub kit (signed
requests/responses, credential PUT with `expected_version` and an open request of the owner), the login-CLI double
(DBL-03) in a real PTY, the auth-page double over HTTPS with a runtime CA, the mail double over IMAPS (magic link in
`Junk`), the model double (`count_tokens`, credential check per kind) and headless Chromium (Playwright for Python).

- Relay (`test_relay.py`): authorize URL relayed, code from a user's HTTP-level browser walk typed into the PTY, token
  read and stored as `claude_subscription {token, email:null, kind:"setup_token"}` authored by the device; hard-wrapped
  token (`--columns 40`); refused code → `code_invalid`; malformed follow-ups → `code_invalid`; follow-up of another
  user refused and audited, the flow goes on; malformed roots → `upstream_error` without starting the CLI; PTY time
  limits (no URL, no end after the code, no code before expiry) → `timeout`; CLI not found, not executable, exiting
  early, unusable URL → `login_cli_failed`; exit mapping (`OAuth error`, other errors, no token); no `CK` →
  `credential_missing`; store refusals/transport errors; replacement by a newer login (old directory removed, late
  follow-up refused, shutdown answered `device_shutdown`); the CLI's environment (allow-list, every path inside the
  temp directory, a TTY, no profile path).
- Automation (`test_automation.py`): full chain in headless Chromium with the magic link in Junk (progress stages,
  `{step:"done", email}`, stored e-mail); fall-back to the relay with `automation:"failed"` and a completed relay for
  a human-verification page, a missing mail and an error page; org device (`LOGIN_AUTOMATION=off`) and missing extra
  fall back without a browser; missing/malformed `mailbox` → `credential_missing`; SPKI pin only after verification
  against `SSL_CERT_FILE`; IMAP retry until the mail wait ends; a closed request is not a fall-back.
- Checks (`test_checks.py`): `credential.check` valid/invalid for API key and subscription token against the model
  double, catalog model, unreadable catalog, missing credential, unreachable/unexpected upstream; `mailbox.check`
  valid, refused login, unreachable, malformed body, missing credential, failed `LIST`.
- Profile (`test_profile.py`): `LOGIN_AUTOMATION` default per device kind and explicit override; `service install
  --profile platform|org` writes the setting into the plist; extra installation and its failures.
- Secrecy: every flow test scans the runner log and all response bodies for token, code and mailbox password; every
  response body is validated against `contracts/requests/<kind>.response.json`; no `login-*` directory remains after
  any test (success, failure, cancellation).

## E2E
E2E-28 (a, b) on T1 is delivered by SCN-05 (the spec does not exist yet); not run in this package.
