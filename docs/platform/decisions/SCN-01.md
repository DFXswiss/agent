# SCN-01 – decisions (M1 scenarios A: E2E-01/06/07/08, PERF-01/02, T1 and T2)

1. **Doubles in the guest (open point of the M0 acceptance)**: reverse SSH tunnel per run VM on loopback, ADR-034.
   The credential contract and the runner stay unchanged (`http://` only to loopback). The guest OS's own NTP (port
   123) and DNS to the device's own resolvers (port 53) through the Lima host agent are allowed by the egress check;
   any other VM connection stays a violation. Tested by `tests/test_full_chain.py` (tunnels) and `tests/test_egress.py`.
2. **Golden image of the E2E runners**: the runner adopts the shared cache `cache-golden-<sha12>` as its ready golden
   (`agent-runner image adopt`, agent repository, `golden.Builder.adopt`) instead of building inside the hermetic run
   (a build needs the package mirrors and would be egress). The cache is never changed or deleted by the runner;
   building it stays `tools/wp/golden-cache.sh` (outside the run).
3. **Operator CLI in the run**: `POST /operator` passes `--key-file` (the public test root seed in the CLI's key file
   format, 0600, in the iteration's scratch) and `--hub <public origin>`. The CLI signs for the public host; in T2 the
   hub device gets an `operator-hub` relay on the public port, opened on first use (not a link of the run, so the link
   list of E2E-00 stays unchanged).
4. **Enrollment**: `POST /runners/{name}/enroll` stops the daemon, runs `agent-runner enroll` with the runner's
   environment and starts it again – the daemon's sync tasks end at once while the device is not enrolled. A restarted
   process gets a fresh log (`<name>.log`; the earlier one is kept as `<name>.<n>.log`), so its ready line and its state
   are read from the new process only (`device.py`).
5. **Platform device of a run**: the first spec that needs it enrolls `runner-1` (`invite-device`, `enroll --platform`,
   `certify-pending --device`); later specs of the iteration reuse the active device (`fixtures/platform.ts`), so every
   spec also runs alone (`--only`).
6. **Warm pool**: with a `@needs-vm` spec selected and no `E2E_POOL_SIZE`, each runner keeps 2 pool VMs. Before a
   timed task the spec waits for the runner's pool *and* the hub's capacity report (`capacity.pool_ready`, sent with
   the 15 s heartbeat), so the scheduler's estimate is the warm-pool one.
7. **Emptying the pool** (`POST /pool/drain {"size": n}`): the runner has no drain command; the route stops the
   daemon, deletes the pool VMs it reports (`pool status`, own prefix only), sets `AGENT_RUNNER_POOL_SIZE=n` and starts
   it again; the start-up reconciliation marks the vanished rows deleted (05 §3.5). The same call with the old size
   restores the pool.
8. **The answer in the chat** (E2E-06/07/08): asserted in the chat of the task page (WEB-05, `chat.output`,
   `chat.prompt.text`); E2E-06 additionally opens the stored `session.output` with the user's own identity (opened
   with the recovery code the onboarding showed) and wg key, i.e. what only a member can read.
9. **Follow-up prompt** (E2E-08): typed into the chat composer of the first task (`chat.composer`).
10. **Inviter's platform invitation** (E2E-01): through `POST /v1/platform/invites` with the inviter's browser session
    (the platform-invitation screen is WEB-07); the refusal before the role grant is asserted too.
11. **"Delayed"** (E2E-07 a): a clone path start is 6–7 s against an estimate of 8 s, so it does not reach 0 by itself.
    The spec cuts the runner's link to the hub once the VM assignment began; the countdown reaches 0 and shows
    "Delayed" with the current step and a running elapsed time; after the link is restored the buffered transitions
    arrive and the task works. Clone path evidence: `vm_ready − vm_assigning > 2 s` (a pool VM takes milliseconds).
12. **No reloads in specs**: a reload asks for the passkey again (WEB-03); specs navigate with the app's links.
13. **Zero-knowledge checks of E2E-01**: every table of schema `hub` is searched (as JSON text, byte columns as hex)
    for the invitation code (with and without dashes), the recovery code and the display name.
14. **PERF harness**: `perf/perf-01-time-to-working.spec.ts`, `perf/perf-02-clone-fallback.spec.ts` (Playwright, tags
    `@PERF-01`/`@PERF-02`), `perf/time_to_working.py` (hub-clock values from the hub database, per-transition breakdown
    of hub receive and device times, CSV `<artifacts>/perf-time-to-working.csv`, p95 nearest rank) and
    `POST /perf/time-to-working`. Runs per series: `E2E_PERF_RUNS` / `E2E_PERF_SHORT_RUNS` (default 5; ACC-M1/PFM-01
    set 20/10); the specs assert the pass criterion. `playwright.config.ts` now matches `specs/` and `perf/`.
15. **Read-only database role of the control API**: `agent-hub migrate` revokes CONNECT from PUBLIC; the orchestrator
    grants it to `e2e_reader` after the migration.
16. **Platform model key of the runners**: a run secret in a 0600 file (`AGENT_RUNNER_OPENROUTER_KEY_FILE`); the model
    double does not check it in these scenarios (key injection is E2E-27, SCN-05).
17. **Golden adoption is TEST_MODE only and checked** (agent: `image adopt`): the cache's tag must be the local tree's
    `cache_tag` (same rule as `orchestrator/golden.py` and the work-package tools), so recorded versions and digest are
    the cache's.
18. **The runner's `session.start` hint and push pacing** (agent decisions SCN-01): a further task of a workgroup
    waited up to 30 s for the next periodic reconcile run (no hint handler), and pushes started 250 ms apart could
    arrive closer at the hub (`429`, `Retry-After: 1`). Both fixed in the agent repository; E2E-08 bounds the start of
    a further task (< 10 s) so the hint stays covered.
19. **Files of SCN-00 changed** (review): `routes/pool.py` (the `501` placeholder of "empty the warm pool" now does it;
    the runner has no drain command and the route cannot move to a new file because discovery refuses a second
    `POST /pool/drain`), `routes/operator.py` (key file, `--hub`, T2 relay), `run.py` (warm pool default, golden
    adoption, tunnels, platform key, reader grant, environment of runner-device processes in T2), `services.py`
    (runner commands, key files), `device.py` (fresh log per restart, `~` expansion), `egress.py` (VM OS rule),
    `fixtures/orchestrator.ts` (client methods), `playwright.config.ts`/`tsconfig.json` (`perf/`), `.coveragerc` and
    the Makefile's `platform-e2e` (`perf` package), test support (`needs_vm` off by default, fake commands). Each change
    is the smallest that makes the full chain run; the unit tests cover all of them (100 %).
20. **T2 environment**: processes on the runner device got the hub device's `HOME`, `TMPDIR`, `USER`, `LOGNAME`; Lima
    keeps its instances below the home, so T2 runner processes now get `HOME="~"` (expanded by the runner device's node)
    and inherit the rest from that device.
21. **PERF onboarding pace**: the hub allows 30 anonymous requests per minute and source address (03-api.md §1, not
    configurable), all browsers of a run share the relay's address and an onboarding makes about six; the PERF series
    space onboardings by 15 s. Onboarding is not part of the measured time.
22. **Countdown digits in E2E-06**: a warm-pool start reaches `working` in under a second, so the digits may be gone
    before the first check; the spec asserts the countdown panel and its one-line summary "Ready in x.y s" (rendered
    only by the countdown) and the steps in order from the hub database. E2E-07 asserts digits, "Delayed" and reasons.
23. **Not done here** (review, low): PRF outputs never reach the hub by protocol and cannot be observed from the test
    (no PRF field exists in the hub schema; the zero-knowledge check covers code, recovery code and name); the
    invitation code is on the `agent-runner enroll` command line (test-only, single-use, expires in 24 h);
    the runs per series come from `E2E_PERF_RUNS` (series "new org", default 5) and `E2E_PERF_SHORT_RUNS` ("new
    workgroup" and PERF-02, default = `E2E_PERF_RUNS`); the orchestrator passes every `E2E_PERF_*` variable on.
