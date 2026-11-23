"""Login doubles for the Claude subscription login (ADR-011, docs/platform/spec/07-testing.md section 9); standard
library only.

- `cli`: the login-CLI double, ``python -m agent_doubles.login_cli setup-token --auth-url URL`` (mimics
  ``claude setup-token``; ``AGENT_RUNNER_LOGIN_ARGV`` points at it in tests).
- `authpage`: the auth-page double (e-mail form, magic link through the mail double, "Authorize", code page, token
  endpoint), ``python -m agent_doubles.login_cli.authpage``.
- `codes`: the deterministic codes and tokens both share.
"""
