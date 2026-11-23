# SCN-05 – decisions (M2c scenarios: E2E-16/27/28/36, LIVE-01/LIVE-03 smoke)

1. **No central list touched for the new services.** The usage double, the login doubles and the live bridge are
   started on first use by control routes (`routes/quota.py`, `routes/login.py`, `routes/model.py` `POST
   /live/upstreams`) and registered in the run context as doubles (`usage`, `mail`, `authpage`), so the run's final
   check asserts their state without starting them in `run.py`/`services.py` (shared with the parallel scenario
   packages). Files outside the package's own paths, each a small change: `orchestrator/run.py` (live mode only,
   decision 14; live specs count for `needs_vm`), `orchestrator/checkout.py` + `tests/support.py` (`login` extra,
   decision 6), `playwright.config.ts` + `tsconfig.json` (the `live/` directory), and new files
   `orchestrator/usage_double.py`, `orchestrator/live_bridge.py`, `fixtures/model.ts`, `tests/test_routes_model.py`,
   `tests/test_run_live.py`.
   The runners learn the new upstreams through their environment and are restarted with the same home (the pattern of
   `routes/idle.py`); the specs wait until every active device is connected again (`devicesOnline`).
2. **Ports** (ADR-027 section 7): usage double (and, in live runs, the live bridge in the same process) `+11`, auth
   pages `+12`, auth-page control `+15`, mail control `+16`, SMTP `+17`, IMAPS `+18`, IMAP `+19` (`+13/+14` belong to
   the GitHub double of SCN-04).
3. **Subscription quota double** (`orchestrator/usage_double.py`, stdlib): the doubles of the agent repository have no
   OAuth usage endpoint (the model double refuses unknown routes, DBL-01 note), and the runner polls a full URL
   (`AGENT_RUNNER_ANTHROPIC_USAGE_URL`), so a small double in the suite serves it: only the exact subscription token
   with `oauth-2025-04-20`; anything else is a failure of its state. E2E-16 asserts the exact token; afterwards it
   accepts any subscription token (later specs connect further subscriptions on the same device).
4. **Credential checks per access kind at the model double** use the double's own `access` check (exact token and
   header per kind: `x-api-key` without `authorization`; Bearer; Bearer + oauth beta) instead of per-step header
   expectations. The values the specs cannot know stay in the orchestrator: the run's platform key is read from the
   runner's key file, the subscription token is derived from the auth page's seed
   (`agent_doubles.login_cli.codes.access_token(seed, 1)`, the first token after the seed).
5. **Subscriptions in E2E-16/E2E-27 are connected through the real relay** (login-CLI double, auth-page double, mail
   double); the web never sees a token, and there is no other product path to store one. The manual step of the
   person (open the link, sign in with the magic link from the mailbox, authorize, copy the code) runs in a separate
   browser context trusting the doubles' test CA; the magic link is read over IMAP by the orchestrator
   (`POST /login/magic-link`, the password only in the reader's environment).
6. **Login doubles setup**: mail double first (creates the runtime CA in the run's scratch), auth page with the same
   CA directory, `--smtp` to the mail double and `--mail-from` with an `anthropic.com` address (the runner's IMAP
   sender filter, RUN-13 decision 12). Runner: `AGENT_RUNNER_LOGIN_ARGV` = the login-CLI double with the absolute
   interpreter and `--ca-file`, `AGENT_RUNNER_LOGIN_AUTOMATION=on`, `SSL_CERT_FILE` = the CA. The E2E agent venv now
   installs the runner's `login` extra (`orchestrator/checkout.py`; marker includes the extras); its Chromium is the
   suite's (`PLAYWRIGHT_BROWSERS_PATH`, same revision 1255 for the Python and the Node Playwright).
7. **E2E-28 failure path**: the auth-page fault `email_page` (human verification instead of the mail) stops the
   automation at once; the web shows `login_automation_failed` with the authorize link; the scenario is then reset
   without fault (the person's own browser is no bot) and the relay completes.
8. **Session environment check** (`POST /sessions/{id}/env-check`): `/proc/<main pid>/environ` of unit
   `agent-session-<id>` read as root in the TEST_MODE golden (ADR-036). Values never leave the orchestrator: the
   answer has names, the non-secret model variables, whether both token variables hold one and the same session token,
   and the variables that contain a credential. Allowed names = 05-runner.md section 10.3 + the session basics of the
   guest (`HOME`, `PATH`, `TERM`, `LANG`, `CLAUDE_CONFIG_DIR`, `PLATFORM_SESSION_TOKEN`) + what systemd sets for every
   unit (`INVOCATION_ID`, `JOURNAL_STREAM`, `MEMORY_PRESSURE_*`, `SYSTEMD_EXEC_PID`, `USER`, `LOGNAME`).
9. **Credential scan** (09-milestones.md M2c, `POST /checks/credentials`): every running run VM (files below `/srv`,
   `/home`, `/root`, `/tmp`, `/var/tmp`, `/var/log`, `/var/lib`, `/run`, `/etc`, `/opt` and the journal), the runner
   device (every runner home including the store's PostgreSQL files and the temporary login directories, and the run
   directory with runner, double and egress logs) and the hub side (`routes/plaintext.py`: dump, hub and PostgreSQL
   logs, browser link). The VM and runner-device scans receive the first 8 characters of each value and the SHA-256
   of the whole value, never the value itself (a command line inside a VM is journaled by sudo).
10. **Test API key shape**: random `e2e-api-key-…` (the contract allows any printable key). A provider-shaped
    `sk-ant-…` key typed into the page ends up in a retained Playwright trace of a failed spec and the artefact scan
    rightly treats every such value as a leaked real key.
11. **Budget step**: the hub is restarted with `AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY=0.001`, below what the org
    spent on the platform in step 1 (the double's fixed OpenRouter cost); preference `platform`; the next session ends
    with `budget_exceeded` before it reaches the model; the budget is reset (and the devices reconnect) in `finally`.
12. **E2E-36 org device** uses the runner instances route of SCN-03 (`POST /runners/{name}/spawn` with `device: org`,
    no platform key file) and the devices screen for the invitation of the default workgroup; the instance is removed
    at the end.
13. **Usage assertions** compare the hub counters with the requests the model double answered (its journal, by step
    ID): tokens = n × the scenario's fixed usage, cost = n × the fixed OpenRouter cost (provider) or the catalog list
    price of each answered request's model (list price, subscription equivalent); the usage strip must show exactly
    these numbers (formatted with the web's own formatters). Title requests of Claude Code count like every request.
14. **Live runs** (07-testing.md section 6; `run.py`, three small additions): the operator's secrets arrive as
    `E2E_LIVE_*` variables of the orchestrator; only Playwright gets them, and they are run secrets of the artefact
    scan. The VMs reach the providers only through the live bridge on the runner device (`orchestrator/live_bridge.py`,
    HTTP on loopback → HTTPS to `openrouter.ai` or `api.anthropic.com`, nothing else) and an extra reverse tunnel of
    its port; in a live run HTTPS from the bridge's own process to the current addresses of these two hosts is the
    only egress beside the doubles (any other process, also a VM's Lima host agent, stays a violation). The spend poll
    and the usage poll of the runner use the bridge, too. Cost cap of LIVE-01: a guard reads the key's daily usage
    every 10 s and ends the test above USD 1.00; because OpenRouter books with a delay, the cost the hub recorded for
    the task is checked against the cap as well.
15. **Secrets of the live smoke are handed over on standard input** of the SSH session to the runner device (the
    remote script reads them before anything else runs); `tools/wp/e2e.sh` has no secret channel, so the smoke ran
    with a workspace driver that reuses its functions (sync, remote script, artefacts) – never a file, an argument or
    a log. The subscription token is deposited as `claude_subscription` (kind `setup_token`, the only kind of the
    contract) sealed in Node under the test user's credential key; the page never holds it.
16. **LIVE-01 "provider audited"**: the smoke asserts the provider recorded with the usage (`last_provider`); the
    runner audits a provider only when it is outside the catalog allow-list (empty allow-list = every provider,
    RUN-12), which the full live test (LVT-01) covers.
17. **R-P.10 in LIVE-03**: the profile check runs where the profile is, on the operator's machine, with the tested
    tool of the agent repository (`scripts/probes/probe_live03.py snapshot|check|token|compare|scan`, PRB-01):
    snapshot (file digests and keychain attributes without the secret) before, the current access token read once
    into a pipe, snapshot after, comparison and a scan of every fetched artefact for the token; the spec on the runner
    device cannot see the profile. The procedure and its result are in `docs/platform/acceptance/SCN-05.md`; the
    in-repository helpers for keychain secrets are LVT-01's (`e2e/platform/live/`).
18. **Review items kept as they are**: "no platform key on org devices" (E2E-36) is enforced by the runner's
    fail-closed configuration (an org device with `OPENROUTER_KEY_FILE` does not start, RUN-12 unit tests), so the E2E
    spec asserts the consequence (`model_access_unavailable` on the org device while the platform device of the run is
    online); quota rows are filtered to their owner by `GET /v1/me/quotas` (HUB-14, WEB-10 tests), E2E-16 asserts the
    owner's snapshot rows and bars.
