# SCN-05 – acceptance evidence (M2c scenarios: E2E-16/27/28/36, LIVE-01/LIVE-03 smoke)

Devices by role only: **runner device** (T1: hub, browser, orchestrator, runners, VMs, doubles) and the operator's
machine (live secrets, subscription profile). Golden image: the shared TEST_MODE cache of the agent integration state
(ADR-036), warm pool 2 VMs per runner; model double on the runner device, reached in the VMs through the guest loopback
tunnel (ADR-034). Further doubles of these scenarios, started by control routes on first use: the subscription usage
double (`orchestrator/usage_double.py`), the mail double and the auth-page double with the login-CLI double as the
runner's `claude setup-token` (decisions 1–6).

## E2E (07-testing.md section 2.1)

| Run | Specs | Result |
|---|---|---|
| `e2e.sh SCN-05 --topology t1 --only E2E-16,E2E-27,E2E-28 --repeat 3` | E2E-16, -27, -28 | 3/3 iterations green (about 61 s, 20 s, 41 s per spec) |
| `run.sh --topology t1 --only E2E-36 --repeat 3` (package branch with the runner instances route of SCN-03) | E2E-36 | 3/3 iterations green |
| `run.sh --topology t1 --only E2E-16,E2E-27,E2E-28,E2E-36` (development confirmations, before and after the review) | all four in one run | green |

E2E-36 needs the runner instances route of SCN-03 (`POST /runners/{name}/spawn`), which SCN-03 brings to the integration
branch (not merged when this package merged); its three iterations ran with that route on the package branch. Standard end checks of
every iteration: no egress violation, every double's state `ok` (model, usage, mail, auth
page), runners alive, audit chain verified (`agent-hub audit verify`), VMs of the run deleted, artefact secret scan
clean.

What each spec proves (beyond the catalog row):

| Spec | Evidence |
|---|---|
| E2E-16 | Three tasks of one private org on the platform device. Platform access: the hub's counters hold n × the scenario's tokens of the n answered requests (title requests included), cost = n × the double's OpenRouter `usage.cost` (basis `provider`, provider recorded), context = input + cache of the latest request ÷ the catalog window. API key (model chosen in the settings): cost = catalog list price of each answered request's model (`list_price`). Subscription (relay login): list price as `subscription_equivalent`. The usage strip shows exactly these numbers. The runner polls the owner's quota at the usage double with the exact subscription token and the oauth beta; the hub holds the owner's `five_hour`/`seven_day` snapshot and the settings show the bars (37.5 %, 12 %) with reset times. |
| E2E-27 | Platform default: session `platform` with `deepseek/deepseek-v4.1-flash-1017`, the double accepts only the run's platform key as Bearer; API key: only `x-api-key` with the user's key and no `authorization`; subscription: only Bearer with the issued token and `oauth-2025-04-20` in `anthropic-beta` (each enforced by the double's credential check, a mismatch is a failure of its state); model IDs as configured (DeepSeek default; `claude-sonnet-5-5`, `claude-haiku-5-5` chosen in the settings). For each session, `/proc/<pid>/environ` of Claude Code: one session token in `ANTHROPIC_AUTH_TOKEN` and `PLATFORM_SESSION_TOKEN`, empty `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` = the gateway, only allowed names, no credential value (platform key, API key, subscription token); DeepSeek in every model slot for the platform. Budget: the hub restarted with a daily budget below the org's platform spend; the next platform session fails with `budget_exceeded` (hub session, `session.failed` audit, web countdown) and the model double receives nothing. |
| E2E-28 | (a) Relay: the authorize link of the login-CLI double in the web, the person signs in at the auth page with the magic link from the INBOX, authorizes and pastes the code; the device stores `claude_subscription` (version 1). (b) Mailbox automation with the magic link in `Junk`: no human step, the device reports the signed-in address, the web shows it (version 2). (c) Human verification at the auth page: the automation gives up, the web shows `login_automation_failed` with the link, the relay completes (version 3). The temporary login home exists while the CLI waits and is gone after each login; all `claude.login` requests answered. |
| E2E-36 | A further runner instance enrolls as an org device of the private org (device invitation of the default workgroup in the devices screen; no platform key there). Without own credentials the session on that device ends with `model_access_unavailable` (hub, web) and the model double sees no request; with the user's API key stored, the next prompt runs on the same device with `api_key` (`list_price`), the environment holds no credential. |

## Credential scan (09-milestones.md M2c)

Every spec ends with `POST /checks/credentials` for the API keys, mailbox passwords, the run's platform key and the
issued subscription tokens: every running VM of the run (files below `/srv`, `/home`, `/root`, `/tmp`, `/var/tmp`,
`/var/log`, `/var/lib`, `/run`, `/etc`, `/opt` and the journal), the runner device (every runner home including the
store's database files and the temporary login homes, the run directory with runner, double and egress logs) and
the hub side (database dump, hub and PostgreSQL logs, every byte the hub sent to the browser). No finding in any
iteration.

## LIVE smoke (07-testing.md sections 2.4 and 6), once

`e2e/platform/run.sh --topology t1 --live --only LIVE-01,LIVE-03` on the runner device; the operator's secrets
(platform OpenRouter key from the keychain, the current access token of one subscription profile) handed over only on
standard input of the SSH session (decision 15).

| Test | Result |
|---|---|
| LIVE-01 | green. First task of a new private org answered by `deepseek/deepseek-v4.1-flash-1017` through the live bridge, then a Write tool turn (`live01.txt` in the task's file events). Hub: 4 requests, cost basis `provider`, USD 0.001981 recorded from OpenRouter's `usage.cost`, provider `Fireworks`. Time to working 0.85 s. Key's daily usage delta during the run 0.0 (OpenRouter books with a delay; the earlier attempt's 0.000874 showed up later); far below the USD 1.00 cap. |
| LIVE-03 | green. One `claude-haiku-5-5` turn with the read-only current access token (session access `subscription`, basis `subscription_equivalent`, USD 0.000725 equivalent); the usage endpoint accepted the token: quota windows `five_hour`, `seven_day` and one weekly model window, the bars shown. Profile: 380 files and the keychain attributes identical before and after (`probe_live03.py compare`: unchanged); the token and the key were found in no artefact. |

Egress of the live run: only HTTPS of the live bridge's process to the two providers; no violation.

## Found and fixed on the way

- Orchestrator: the E2E agent venv lacked the runner's `login` extra (no Playwright for the mailbox automation).
- Spec side: devices restarted for a changed environment must be online again before a device request; systemd's own
  unit variables belong to the session environment; a provider-shaped test key would appear in retained traces.
- Live mode: live specs did not count for the golden image/warm pool; the tunnel watcher state was read with an
  unknown process name.
- Integration gap reported by the parallel packages and fixed there: the model double now answers the runner's
  spend poll (`/v1/key`).
