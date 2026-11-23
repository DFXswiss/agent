# SCN-00 – E2E orchestrator, topologies T1/T2, virtual authenticator, traceability

Decisions taken where `07-testing.md` sections 1–9, `01-requirements.md`, `10-work-packages.md` sections 5 and 7 and
ADR-027 sections 4 and 7 were silent or ambiguous.

## Topology and network

1. **One public hub origin, reached through relays.** The hub signs and checks the host line of
   `AGENT_HUB_PUBLIC_URL`, the runner signs the host of `AGENT_RUNNER_HUB_URL`, and the web client refuses a
   configuration whose `origin` is not `location.origin`. So browser and runners must all use `http://localhost:<public>`.
   The hub listens on its own port; the **runner relay** listens on `127.0.0.1:<public>` on the runner device and
   forwards to the hub (T2: over the LAN to the hub device's LAN address, so the runner's test-mode rule "plain HTTP only
   to loopback" holds); the **browser relay** listens on another port and Chromium maps the origin to it with
   `--host-resolver-rules=MAP localhost:<public> 127.0.0.1:<browser>`. Every link (`runner-hub`, `browser-hub`, one per
   double) is a relay, so it is logged and can be cut and restored (fault switches of section 9).
2. **Ports.** A run uses `WP_PORT_BASE+4 … +19` (`+0 … +3` stay free for the package's unit-test cluster and the web
   suite): control `+4`, PostgreSQL `+5` (TCP on 127.0.0.1 only, scram passwords, no unix socket), hub `+6`, public `+7`, browser relay `+8`, model double `+9`, its relay `+10`.
3. **Hermetic rule, literally.** Every process gets an allow-list environment with `HTTP(S)_PROXY`/`ALL_PROXY` on the
   closed loopback port 9 and `NO_PROXY` = loopback names (07 §5.2); Chromium gets `--proxy-server=http://127.0.0.1:9`
   with a loopback bypass. The "egress-logging proxy" of §5.1 is realised as (a) the relays, which log every connection
   to the hub and the doubles, and (b) a sampler that reads the TCP/UDP connections of every process the run started
   (and their descendants) with `ps`/`lsof` every 0.5 s. A remote address that is neither loopback nor a device of the
   topology (T2: the other device and the device's own LAN address) is a violation and fails the run. A packet-level
   capture would need root on the work devices; not used.
4. **T2 control channel.** The orchestrator on the second device drives the runner device over **one SSH connection per
   iteration** running `python -m orchestrator.node` (JSON lines on stdin/stdout). When the connection ends, the node
   stops every process it started, so nothing of a run survives a broken link. SSH uses batch mode, `ControlPath=none`
   and its own known-hosts file in the artefact directory. Paths sent as `~/…` are expanded on the device that uses
   them (the homes differ between devices); the agent and agent-core checkouts on the runner device are found at the
   same home-relative paths (`E2E_REMOTE_AGENT_SRC`/`E2E_REMOTE_CORE_SRC` override).
5. **SSH credentials for T2** are a property of the caller, not of the suite: `tools/wp/e2e.sh` (workspace tools, not
   in this repository) now starts a temporary `ssh-agent` with the identity it uses for the runner device and forwards
   it on the connection to the second device (`-A`, own agent socket). No key is copied to any device.

## Run sequence

6. **`--repeat N` = N complete consecutive runs** with fresh services (cluster, hub database, doubles, runners, relays)
   each time, not Playwright's `--repeat-each`; the run stops at the first red iteration. This is what the acceptance
   criteria ("three consecutive runs") need for later VM scenarios.
7. **Web build.** The orchestrator builds the web unminified into the hub's static directory before the hub starts
   (WEB-01 decision 6: browser coverage maps onto the sources only without minification) with the TEST platform root key
   read from `web/build/manifest.ts`, the same key the hub gets. A checkout without `.git` (the copies `tools/wp/remote.sh`
   synchronises) gets the placeholder commit `0…0` and `SOURCE_DATE_EPOCH=0`.
8. **Browser coverage** uses the WEB-01 fixtures unchanged (`web/e2e/fixtures/test.ts`: CSP listener + V8 coverage);
   Playwright runs with the iteration's `web-coverage/` directory as working directory, so the raw data lands there and
   the global teardown prints a summary. Informative only (07 §3); the web gate is `make platform-web`.
9. **Dependencies of the TypeScript part** come from `web/node_modules` through the link `e2e/platform/node_modules`
   (created by `run.sh`/the orchestrator and by `make platform-e2e`); `e2e/platform/package.json` only declares the ES
   module type. WEB-01 stays the only owner of web dependencies (ADR-027 §6).
10. **Model double boot scenario.** The double starts with `orchestrator/idle-scenario.json` (no request expected) because
    a scenario must have at least one step. Final check per double: with a spec scenario loaded `state.ok` must hold; with
    the idle scenario still loaded no request may have arrived. Specs load their scenario through the control API.
11. **Runner readiness** = the daemon's fact line `runner.run … enrolled=no` (not enrolled: it waits for enrollment); the
    control API reports such a runner as `waiting_for_enrollment`. Before the runners start, the runner device must reach
    `/v1/health` of the hub through the runner relay (T2: proves the LAN path).
12. **Final checks**: Playwright exit code, doubles (decision 10), runners still alive unless stopped through the control
    API, egress (decision 3), hub audit chain via `agent-hub audit verify` once that command exists (HUB-16; until then a
    note), VM cleanup of the run prefix `<AGENT_RUNNER_VM_PREFIX>e2e-` with a leftover check, artefact secret scan (the
    run's generated secrets – hub signing key, database passwords – and well-known credential shapes, also inside
    Playwright trace zips). Any failure makes the run red; teardown steps all run even after a failure.
13. **Runner VM prefixes** are `<prefix>e2e-r<n>-` per runner instance; the golden cache is ensured only when a selected
    spec title carries `@needs-vm` (`python -m orchestrator.golden`, same hash, lock and name rule as
    `tools/wp/golden-cache.sh`). The warm-pool fill is the runner's job once VM-02 exposes it.
14. **VM budget** default = device memory − 32 GiB (the runner's default reserve), overridable with
    `E2E_VM_MEMORY_BUDGET_MIB`; planned memory = runners × `E2E_POOL_SIZE` × `E2E_VM_MEMORY_MIB`.

## Control API

15. Routes (one module each in `orchestrator/routes/`, discovered): `GET /state`, doubles (`state`, `scenario`, `reset`,
    `request` – one request to a double on its own device, for seeding and probes), `links` (cut/restore), `runners`
    (stop/start with the same home), `POST /operator` (`agent-hub platform …`), `POST /hub-db/query` (one read-only
    statement, `default_transaction_read_only`), `POST /clock/advance`, `POST /pool/drain`, `POST /hooks/fault`. Features
    of later packages answer **`501 not_available`** naming the package (operator CLI: HUB-04; pool drain: VM-02; fake
    clocks: first expiry scenario SCN-03; hook fault switch: SCN-02/GST-03).

## Traceability

16. `traceability.json` mirrors all 153 rows of `01-requirements.md` (a unit test compares them row by row). Non-ID test
    texts are normalised to gate IDs: `GATE-MATRIX` ("this matrix; M3 acceptance"), `GATE-COVERAGE`,
    `GATE-TRACEABILITY`, `GATE-BRANCH-PROTECTION`; ranges (`ATK-01..ATK-40`, `E2E-01..E2E-39`, `LIVE-01..04`) are expanded.
17. **Milestone per test** = the milestone of the package that writes it first (10 §5, §7); **milestone per requirement**
    = the milestone of its earliest test (M0: 24, M1: 39, M2: 84, M3: 6). LIVE-01/03 are M2 (SCN-05 writes the first
    spec; PRB-01's probe is no suite test), PERF-05 M3. E2E-00 (this smoke) is an extra M0 test without requirement.
18. **Existence.** E2E/PERF/LIVE tests exist when a string literal of a spec under `specs/`, `perf/` or `live/` carries
    their tag; a requirement tag in a title must list every test ID of that title. Gate tests exist through evidence paths
    (`agent-core:` always checked, `agent:` only when an agent checkout is given). `--milestone N` requires every test and
    requirement due up to N; without it only the structure is checked (that is the E2E pre-check).

## Gates

19. `make platform-e2e` (new step of `make platform-ci`): ruff, ruff format, `mypy --strict` for the orchestrator and its
    tests, ESLint/Prettier/`tsc` for the specs and fixtures, pytest with coverage 100 % (lines and branches, separate
    `e2e/platform/.coveragerc`, `patch = subprocess`) including `test_traceability.py` (milestone 0). The GitHub
    workflows are not changed by this package (they belong to FND-00/WEB-01 and would need web dependencies in the hub
    job); `make platform-ci` is the binding local gate (07 §8).

## Review (independent subagent) – changed

20. **Sampler coverage and fail-closed behaviour.** One-shot commands (migrate, operator CLI, golden build, audit verify)
    are sampled while they run; processes whose command line carries the run's VM prefix or `cache-golden-` are sampled
    too (Lima host agents detach from the runner). A failing `ps`/`lsof`, an exception in the sampler thread, or a run
    without one successful sampling round is recorded as a violation and fails the run.
21. **Fresh records.** The iteration directory is emptied at start; the remote node empties its run directory at start.
22. **Teardown never stops early**: every step catches every exception and records it; a lost console does not abort it;
    `summary.json` is written before the artefact scan and scanned with the rest; failure texts and notes have the run's
    secrets replaced by `[run secret]`. Per-iteration pre-check failures exit with 3.
23. **Lost T2 connection**: the remote node also deletes the run's VMs (run prefix) and the runner scratch (runner
    homes) when its stdin ends; every iteration first deletes leftover VMs of the run prefix.
24. **Timeouts are exit code 124** (not 127 "not installed"), so a hanging `limactl` fails the VM cleanup and budget.
25. **Access to run services**: PostgreSQL requires passwords (scram); the control API requires a per-run bearer token
    (`E2E_CONTROL_TOKEN`, which also blocks cross-origin requests of pages in the browser); `/hub-db/query` runs as the
    role `e2e_reader` (`pg_read_all_data`) in a read-only session instead of the superuser.
26. **T2 details**: the hub's LAN address is part of the port pre-check; the known-hosts file lives next to the
    artefacts (kept between runs, `accept-new` only on first contact over the LAN); the T1 checkout preparation gets the
    normal environment (uv may need the index; it is not part of the hermetic run).

## Review – accepted residual risks

27. Connections shorter than the 0.5 s sampling interval, DNS lookups (mDNSResponder) and VM traffic over vz NAT are not
    visible to the sampler; hermeticity rests on the dead proxy for every process, the relays for all hub and double
    traffic, and – from the first VM scenario on – the VM nftables rules of the test images (07 §5.2). A packet capture
    would need root on the work devices.
28. Blocked attempts at the dead proxy are not logged by destination (07 §5.1 "egress-logging proxy": realised as the
    relays plus the sampler, decision 3).
29. The temporary ssh-agent of `tools/wp/e2e.sh` (T2) holds the identities this machine uses for the runner device for at
    most 2 h and is killed at the end of the call; destination constraints (`ssh-add -h`) would need the runner device's
    host key under its LAN address in this machine's known hosts and are not used.
30. In T2 the hub listens on the hub device's LAN address without access control beyond the protocol's own signatures;
    a runner-device process could reach it without the `runner-hub` relay (test topology on a private LAN only).
31. A tag in a skipped spec (`test.skip`) counts as written for traceability; acceptance packages check the Playwright
    results, not only the tags.
