# ADR-011: Claude subscription login

Status: Accepted (2026-11-21)

## Context
- KONZEPT §4: reuse the existing "SMTP e-mail login automation" of the `agent` repository for Claude authentication.
- Correction: `agent/src/agent_cli/account_login.py` reads mail via **IMAP** (polls INBOX and junk for the magic link) and is **semi-manual**: a human pastes the e-mail address and clicks "Authorize" in the normal desktop browser (`open`/`xdg-open`). `opener` and `clipboard` are injectable.
- Runner devices are headless; existing subscription profiles on any machine must never be logged out, re-logged or have refresh tokens copied (`PROJECT.md` hard rule).

## Decision
Two ways to store a `claude_subscription` credential, both run on the execution device host (never in a VM, never in an existing profile):
1. **Setup-token relay (default)**: the runner runs `claude setup-token` in a PTY with a temporary `CLAUDE_CONFIG_DIR`, sends the authorize URL to the user as an encrypted device response, the user authorizes in their own browser and pastes the shown code into the web UI (second device-request message), the runner types it into the PTY, captures the long-lived token, stores it encrypted under the user's CK and deletes the temporary directory.
2. **Mailbox automation (`agent-runner[login]`; enabled by default on platform devices through the platform install profile, off by default on org devices)**: if the user stored a `mailbox` credential for the subscription address, the runner drives the authorize pages with a headless browser and fetches the magic link via IMAP, reusing `agent_cli.account_login.newest_magic_link` (called with a `Mailbox` value built from the decrypted credential); on any page or timeout failure it falls back to 1 (`login_automation_failed`).
- Hermetic tests use the login-CLI double, an authorize-page double and the mail double. **No live test of any login flow**; the LIVE subscription test only uses a current access token read-only.

## Consequences
- No SMTP component is needed; mailbox credentials are confidential and live in the CK scope.
- Automation 2 is the reuse KONZEPT §4 asks for; it is brittle against page changes and bot detection, therefore it always has fallback 1 and is off by default on org devices (deliberate deviation from "reuse as the only path").
- Terms of use of subscriptions on shared servers are the user's/DFX's responsibility (noted risk).

## Alternatives considered
Users paste raw OAuth credentials (exposes refresh tokens), login inside the VM (needs network and a browser in the sandbox), GUI browser on the runner (headless devices).

## Deviation from DESIGN.md
None.
