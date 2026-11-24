# SCN-00 – acceptance evidence

Package: E2E orchestrator, topologies T1/T2, virtual authenticator, traceability (`e2e/platform/`).
Decisions: `docs/platform/decisions/SCN-00.md`.

| Criterion | Evidence |
|---|---|
| Orchestrator unit tests 100 % in `make platform-ci` | `make platform-e2e`: 104 tests, lines and branches 100 % (`e2e/platform/.coveragerc`), ruff, `mypy --strict`, ESLint, Prettier, `tsc` |
| `test_traceability.py --milestone 0` green | `python e2e/platform/test_traceability.py --milestone 0` → green (153 requirements mirrored, 24 due in M0); also run by `make platform-e2e` |
| T1 `--only E2E-00 --repeat 3` green | three consecutive complete runs on the runner device: 3/3 green, 3 smoke tests each (PRF 32 B on `create()` and `get()`, model double scenario consumed, runner waiting for enrollment, USB/PRF-less/UV-refusal variants) |
| T2 `--only E2E-00 --repeat 3` green | hub, PostgreSQL, browser and orchestrator on the second device, doubles and runner on the runner device over the LAN: 3/3 green |
| Egress log shows only allowed targets | every iteration: `egress-summary.json` without violations (links `browser-hub`, `runner-hub`; sampler records loopback only, plus the peer device in T2) |
| No leftover VMs | VM cleanup of the run prefix after every iteration; no instance of the package prefix left on the runner device |
| CON-04 named pre-check with test | `orchestrator/prechecks.py:contracts_version`; tests `test_prechecks`, `test_node_prechecks_stop_the_iteration`; observed live: a run on an outdated branch stopped with `pre-check CON-04 failed` |
| Gates | `tools/wp/gates.sh SCN-00` green (platform-lint, -types, -test, -contracts, -web, -e2e) |
| Independent review | findings high/medium fixed or recorded (decisions 20–31) |
