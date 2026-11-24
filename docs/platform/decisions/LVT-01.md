# LVT-01 – decisions (live tests LIVE-01..LIVE-04)

Scope: `07-testing.md` sections 2.4 and 6 against the real providers. Devices by role only (runner device, operator's
machine); accounts as "test account". Builds on the SCN-05 smokes (`docs/platform/decisions/SCN-05.md` 14–17).

1. **Specs and helpers** (`e2e/platform/live/`): `live-01-openrouter`, `live-02-github`, `live-03-subscription`,
   `live-04-api-key` (the SCN-05 smokes `live-01-smoke`/`live-03-smoke` became the full tests), `helpers.ts`
   (operator values, credentials sealed in Node, cost guard, GitHub client and cleanup) and the operator-side driver
   `driver.py` (Python package `live`, 100 % covered in `make platform-e2e`; `.coveragerc` and the mypy list of the
   Makefile name it). Outside the package's own paths, small extensions of the SCN-00/SCN-05 orchestrator files:
   `cli.py` (`--live-stdin`), `run.py` (live hosts, settings, journaled peers, live values kept from the checkout
   preparation), `live_bridge.py`/`usage_double.py`/`routes/quota.py` (GitHub upstreams), with their tests; the new
   control routes are a discovered file (`routes/live.py`, ADR-027 section 4).
2. **Secret channel** (07 section 6 "secrets only from the keychain or environment of the operator, never in files,
   logs or artefacts"): the driver runs on the operator's machine, reads each secret read-only (environment first,
   then a keychain item `security find-generic-password -s <service> -w`, the GitHub token from the output of the
   operator's `gh auth token`, the subscription token through the agent repository's tested LIVE-03 probe) and writes
   them only to standard input of the operator's transport command. `run.sh --live --live-stdin` (new option of the
   orchestrator CLI) reads `E2E_LIVE_*` (secret) and `AGENT_LIVE_*` (setting) lines from standard input; nothing else
   is accepted and a bad line is reported without its content. `tools/wp/e2e.sh` has no secret channel (it passes no
   `E2E_LIVE_*` variable on purpose: the value would be visible in the SSH command line), so the operator's transport
   is a small workspace script around the same helpers (sync, remote script, device slot, artefacts).
3. **Settings vs. secrets**: the orchestrator hands `AGENT_LIVE_*` to Playwright in live runs, but only `E2E_LIVE_*`
   values are scanned for: the repository name (`AGENT_LIVE_GITHUB_REPO`) appears legitimately in results and URLs.
4. **Artefact scan after every run**: on the runner device by the orchestrator (as before) and again by the driver
   on the operator's machine over the fetched artefacts (zip members included) and the command's log, for every
   secret value; a finding fails the driver. Credentials are sealed in Node (never typed into the page), so no secret
   reaches a Playwright trace.
5. **GitHub through the live bridge** (`orchestrator/live_bridge.py`): upstreams `github` (`api.github.com`) and
   `codeload` (`codeload.github.com`) were added; `POST /live/github` points the runners' GitHub API URL at
   `<bridge>/live/github` and their web URL at the bridge's origin. The runner follows a tarball redirect only to
   `<web URL>/codeload/...` (`agent_runner.github.client.codeload_allowed`), so the bridge rewrites a redirect to
   `https://codeload.github.com/...` into its own `/codeload/...` path and forwards that to codeload. The usage double
   (which hosts the bridge) now also forwards `PATCH` (push) and `DELETE` (the spec's cleanup). The runner code is
   unchanged; its loopback `http://` URLs are accepted because E2E runs are test mode.
6. **Egress of the bridge**: GitHub answers from changing addresses, so a resolution at check time can miss the
   address the bridge used. The bridge journals the peer address of each upstream connection (the TLS host-name check
   proved that it belongs to a live host); the egress check of a live run accepts HTTPS from the bridge process to
   the resolved addresses and to these journaled ones. Every other process stays a violation.
7. **LIVE-02 credential**: no GitHub App exists, so the device flow cannot run against GitHub (E2E-20 covers it
   hermetically, residual risk in the report). The token of the test account's existing gh login is deposited as the
   user's `github` credential (`login`, `user_id`, `scopes` from `GET /user` and `x-oauth-scopes`), sealed in Node
   under the user's credential key, after a first task assigned the platform device (so the record reaches the device
   at once; the spec waits for it in the runner's credential cache).
8. **LIVE-02 session is scripted** by the model double (E2E-21's scenario with the live repository, branches and the
   pull-request number): LIVE-02 tests GitHub, not the model; the scenario keeps the chain deterministic and free.
   The next pull-request number is the repository's highest issue/PR number + 1 (the repository is used only by
   this test).
9. **LIVE-02 differences to E2E-21**: `github.list_repositories` uses `GET /user/installations`, which GitHub refuses
   for an OAuth user token (`403` "must authenticate with an access token authorized to a GitHub App"); the operation
   therefore ends `upstream_error` and the spec asserts exactly that (with an App user token it lists, E2E-21).
   A real `422 Reference already exists` (B-13) cannot be provoked by asking for the same branch twice - the second
   request is a `duplicate` of the dedupe slot `(repo, branch)` and never reaches GitHub - so the spec creates a
   branch at GitHub first and the session asks to create it: the runner must answer `branch_exists`.
10. **LIVE-02 cleanup** at the start (leftovers of earlier runs) and in `finally`: close every open pull request whose
    head starts with `live02/`, delete every branch with that prefix; the test fails if anything is left.
11. **Test repository**: no suitable private repository of the test account existed (all are working repositories),
    so exactly one private repository was created for LIVE-02 (allowed by the client's decision 4 of the project
    rules); its name is operator configuration (`AGENT_LIVE_GITHUB_REPO`), not part of the repository.
12. **LIVE-01 provider audit** (B-13, SCN-05 decision 16): `POST /live/catalog` gives the runners a copy of their
    packaged catalog whose platform provider allow-list names only a label no provider has, so the real provider
    OpenRouter routes to is outside the list; the spec asserts the runner's audit `model.provider_outside_allowlist`
    with that provider. The gateway only flags (ADR-010); restricting providers is the key's setting at OpenRouter.
13. **LIVE-01 interrupt and prefixed prompts** (B-14): after the tool turn, a long turn is interrupted with the
    web's Stop (`interrupt`); the session must be `idle` again (05 section 6.3; GST-06 decision 11 assumed that Claude
    Code sends no Stop hook). Then prompts starting with `?`, `#` (two lines) and `&` must be answered as plain
    prompts - delivered by bracketed paste because typing them would switch Claude Code's input mode. Run with
    DeepSeek, the cheapest live model; the behaviour under test is Claude Code's TUI, not the model.
14. **Cost cap** (`CostGuard`): the key's daily usage is read through the bridge every 10 s; growth above USD 1.00
    closes the page (ends the test); OpenRouter books with a delay, so the cost the hub recorded is checked as well.
15. **LIVE-03 profile check in a shared profile**: every subscription profile of the operator's machine is in use by
    other agents' sessions during a live run, so their files change all the time (SCN-05 live-2; in this package's
    runs: transcripts, shell snapshots, settings sync, `.claude.json`, `sessions/`, plugin caches). The live test's
    only access to the profile is the read of the access token before the run. The driver therefore takes three
    snapshots with the agent repository's probe: before the read, right after it, and after the run. Before/after
    the read is compared strictly - a keychain attribute change or any file change fails, except below path prefixes
    the operator names with `--volatile` for the seconds between the two snapshots (session files of the other
    processes). Before/after the run must show the keychain item unchanged (no refresh, login or logout - by anyone);
    file changes over the run are reported as the other processes' session activity, not judged. The token must be
    valid for 2 h (the probe never refreshes). This deviates from "hash profile files before and after the run" of
    07 section 6 only for files the test provably does not touch after its single read.
16. **LIVE-04** runs only with `E2E_LIVE_ANTHROPIC_KEY`; without a key it is skipped with the reason "no key" and the
    skip is written to its result file (not a failure, 07 section 2.4 "optional").
17. **Keychain isolation of `claude setup-token`** (B-13) is not tested live: running it starts a login, which the
    project rules and ADR-011 forbid in live tests. Residual risk, see the acceptance document.
18. **Review outcomes (independent review, 9 medium findings)**:
    - The operator's live values never reach a process other than Playwright: the orchestrator's checkout preparation
      gets the environment without `E2E_LIVE_*`/`AGENT_LIVE_*`; the driver's children (keychain, gh, probe, transport,
      fetch) get the operator's environment without them; settings are an allow-list (`AGENT_LIVE_GITHUB_REPO`).
    - The bridge's port is tunnelled into the run's VMs (for the gateway). The GitHub upstream therefore has a per-run
      secret name (`github_<random>`, only the orchestrator and the runner's configuration know it), the codeload path
      answers only a redirect URL with GitHub's short-lived `token` query, and `/live/codeload/` is refused: a guest
      reaches no GitHub through the bridge.
    - Cost guard fails closed (three unreadable usage reads in a row trip it) and uses the key's all-time `usage`
      (same growth as `usage_daily`, no reset at midnight UTC); tripping also points the runners back at the model
      double. The rest of the run (LIVE-02/03/04) does not spend on the platform key.
    - GitHub cleanup tries every step and collects failures; the driver repeats it after the run directly at GitHub
      (`--github-cleanup live02/`, also when the spec was cut off) and refuses a repository that is not private.
    - LIVE-02/03/04 run the credential scan of the run (`/checks/credentials`: VMs, runner device, hub dump) for
      their secret.
    - `--volatile` takes only relative path prefixes (no `..`, no hidden first segment): directories such as
      `projects/` and the settings-sync files every running Claude Code rewrites (`remote-settings.json`,
      `policy-limits.json`, seen changing during a run); the token must be valid for 2 h (the run).
    - Kept: the TypeScript helpers have no offline unit tests - like every Playwright fixture of the suite they are
      exercised by the runs that use them (each live run executes all of them); the deterministic, risky logic of the
      operator side (secret hand-over, scan, profile verdict, fallback cleanup) is the Python driver, 100 % covered.
      The access token is stored with `kind: "setup_token"`, the only kind of the credential contract (SCN-05 decision
      15), as the user's own credential in the test user's private org (the "test org" of 07 section 2.4). The
      keychain snapshot sees attributes, not the access list: the `-w` read must run without an access prompt (probe
      documentation). The next PR number assumes a repository only this test uses (decision 8).
