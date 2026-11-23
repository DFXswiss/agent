# Live tests LIVE-01..LIVE-04 – acceptance evidence (LVT-01)

Specification: `07-testing.md` sections 2.4 and 6. Decisions: `docs/platform/decisions/LVT-01.md`. Devices by role
only: the **runner device** (topology T1: hub, browser, orchestrator, runners, VMs, doubles and the live bridge) and
the **operator's machine** (secrets, the Claude subscription profile, the transport to the runner device). Accounts
are named "test account"; no secret value appears in any result file.

## How it runs

```
python -m live.driver --result R --artifacts A --log L --fetch "<copy artefacts to A>" \
    --openrouter-service <keychain item> --github-token-command "gh auth token ..." \
    --profile <chosen profile> --exclude-profile <excluded> --volatile projects/ ... \
    --probe <agent>/scripts/probes/probe_live03.py --probe-python <agent venv python> \
    -- <ssh to the runner device> "... e2e/platform/run.sh --topology t1 --live --live-stdin --only LIVE-01,LIVE-02,LIVE-03,LIVE-04"
```

Operator settings: `AGENT_LIVE_GITHUB_REPO` (the private test repository of the test account),
`AGENT_LIVE_EXCLUDED_PROFILES`; optional secrets from the environment `AGENT_LIVE_OPENROUTER_KEY`,
`AGENT_LIVE_GITHUB_TOKEN`, `AGENT_LIVE_ANTHROPIC_API_KEY`. Never part of `make platform-ci`.

## Result (final run of all four on the merged code, 2026-11-22)

| Test | Result | Evidence |
|---|---|---|
| LIVE-01 | green | E2E-06 on a new private org: answer "4", first working 2.4 s (informative; PERF-01 owns the < 10 s criterion). Write tool turn, `live01.txt` reported as session file. Interrupt of a long turn with Stop: session `idle` again 0.23 s after the click. Prompts starting with `?`, `#` (two lines) and `&` answered as plain prompts by real Claude Code. Usage: 7 requests, cost basis `provider`, USD 0.002033 recorded by the hub from OpenRouter's `usage.cost`, provider recorded (`Fireworks`). Provider audit: with an allow-list naming no real provider the runner wrote 6 audit rows `model.provider_outside_allowlist` (provider `Fireworks`, model `deepseek/deepseek-v4.1-flash-1017`). |
| LIVE-02 | green | E2E-21 at the real GitHub (private test repository, checked private before any write; token of the test account's existing login deposited as `github` credential). 31 REST calls through the bridge exactly in the expected order and status: `GET /user/installations` 403 (an OAuth user token is no GitHub App token → `github.list_repositories` `upstream_error`), checkout with tarball redirect to codeload, **real `422 Reference already exists` → `branch_exists`**, branch, blob/tree/commit, compare + fast-forward `PATCH`, draft pull request, comment, read back. Every operation has its hub record with params/result events, state and final audit row; at GitHub the branch head equals the committed SHA, the PR is an open draft with the session's comment; the token is in no VM, runner-device file or hub dump (credential scan). Cleanup closed the PR and deleted both branches; the driver's fallback cleanup afterwards found nothing; the repository has only `main` and no open PR. |
| LIVE-03 | green | Profile chosen by the operator (not the excluded one), current access token read once (validity > 2 h, no refresh, login or logout). Haiku-class turn (`claude-haiku-5-5`) through gateway and bridge: answer "pong", access `subscription`, basis `subscription_equivalent`; the token is in no VM, runner-device file or hub dump. **Usage endpoint: accepts the access token**, quotas shown (windows `five_hour`, `seven_day`, `weekly:<model>`). Profile: identical before and right after the token read (files and keychain attributes, no exception needed); keychain item unchanged over the whole run; 14 files changed over the run by other sessions running on the same profile (decision 15). |
| LIVE-04 | not run – optional, not needed | No Anthropic API key, by the owner's decision (the Claude access runs through the subscriptions); optional per 07 section 2.4, not an open point. The run records the skip (`live-04.json`: `skipped: no key`). R-4.3 (API-key access) is evidenced by E2E-27 against the model double; Claude live is evidenced by LIVE-03. |

Cost: the key's usage grew by USD 0.0019 (the hub recorded USD 0.0020) – far below the cap of USD 1.00; the guard
never fired. Secret scan: the orchestrator's scan on the runner device and the driver's scan of the fetched artefacts
(zip members included) and of the run log found none of the secret values. Egress: only the doubles and the live
bridge's HTTPS to the live providers; no violation. Run end checks: every double `ok`, runners alive, audit chain
verified, the run's VMs deleted.

## All runs of this package

| Run | Result | What it showed |
|---|---|---|
| LIVE-02 alone (1) | red | The checkout directory is `repos/<owner>/<name>` in lower case; the scripted Write went to the mixed-case path, the commit saw no change and GitHub refused the empty tree (`422`) → `upstream_error` (open finding below). Cleanup worked. |
| LIVE-02 alone (2) | green | – |
| all four (1) | red (LIVE-02) | LIVE-01 had pointed the runners' model upstreams at the real provider; LIVE-02 now points them back at the model double first. LIVE-01, LIVE-03 green. |
| all four (2) | green | First complete green run (before the review fixes). |
| all four (3) | red (LIVE-01) | After the interrupt the `?` prompt was refused twice by the guest - `unregistered`, then `delivery_failed` (open finding below). Profile check flagged settings-sync files of other sessions. |
| all four (4) | green (E2E), driver red | All tests green; the profile changed under other sessions (`.claude.json`, `sessions/`, plugin caches) - led to decision 15 (strict check around the read, keychain over the run). |
| all four (5) | red | Pre-check CON-04: the integration branches had moved (contracts); the package was rebased. |
| all four (6) | green | The final run above. |

## Residual risks and open findings

- **GitHub device flow** never ran against GitHub: no GitHub App exists (out of scope). Covered hermetically by E2E-20.
  `github.list_repositories` needs a GitHub App user token; with the OAuth token used here GitHub refuses it (403).
- **Keychain isolation of `claude setup-token`** not tested live: running it starts a login, which live tests must
  not do (ADR-011). The setup token at the usage endpoint is likewise untested (LIVE-03 used an access token).
- **Provider restriction** is detected and audited, not enforced by the gateway (ADR-010); the restriction itself is
  the key's setting at OpenRouter.
- **Prompt after an interrupt can be refused** (B-14, seen in 1 of 5 runs that reached it): after Stop on a long
  turn the next prompt (`? …`, pasted) was refused by the guest as `unregistered` - the UserPromptSubmit hook saw a
  text whose hash was not the registered prompt - and on the retry as `delivery_failed`; the user's prompt is lost.
  Likely cause: Claude Code puts the interrupted prompt back into its input box and the clearing before the paste
  (`C-e C-u`) does not remove it completely, so the submitted text is a mix. In the other four runs the same steps
  passed (interrupt → `idle` in 0.18-0.52 s, all three prefixed prompts answered). Owner: guest delivery (agent).
  Fixed by KOR-09: the input is emptied until it reads empty, checked before `Enter`, and the delivery starts again
  after a hook refusal (`05-runner.md` §6.2).
- **Empty commit**: `github.commit` without any change reaches GitHub, which refuses the empty tree (`422`); the
  session gets `upstream_error` "GitHub refused the request." instead of a formal refusal. The specification
  (03-api.md section 9) does not define the case. Fixed by KOR-10: a commit without a change is the formal refusal
  `no_changes` (03-api.md section 9), and the checkout result's `dir` names the lower-case path.
