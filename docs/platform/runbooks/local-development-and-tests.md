# Runbook: local development and tests

Scope: setting up the checkout, the binding local gates and the E2E suite
([07 §1, §3, §5, §7](../spec/07-testing.md#3-coverage-gates), ADR-027 §6). GitHub workflows `platform-*.yml`
run the same commands; when GitHub Actions are unavailable the local gates are binding.

## 1. Setup

One checkout of `DFXswiss/agent` (the former agent-core is part of it since ADR-061), on the integration branch or a
branch of it.

```bash
uv venv --python 3.12 .venv && . .venv/bin/activate
pip install -e ".[platform,hub,platform-test]"   # agent-hub, agent-runner, agent-guest, agent-platform, doubles
cd web && npm ci && cd ..                        # Node version in web/.nvmrc
```

Tests need a real PostgreSQL ≥ 16 (no mocks, never skipped):

| Tests | Variable | Content |
|---|---|---|
| hub (`tests/platform/hub/`) | `AGENT_HUB_TEST_POSTGRES` | superuser DSN of a disposable local cluster; each test database `t_hub_<hex>` with owner/writer roles is created by the fixture |
| runner (`tests/platform/runner/`) | `AGENT_RUNNER_TEST_PG` | DSN of a disposable cluster (else `initdb` per test session) |

For the hub tests a missing variable fails the suite (`pytest.fail`); the runner tests start their own cluster with `initdb` instead. One cluster may serve both. Use only loopback clusters you own.

## 2. Gates

| Command | What it runs |
|---|---|
| `make platform-ci` | `ruff check` + `ruff format --check`, `mypy --strict`, pytest of hub, runner, guest, doubles, agent tool, probes library and monitoring helpers with **one** gate of 100 % lines and branches (`.coveragerc-platform`, combined with the Linux part), contract version check, web gates (`npm run lint`, `typecheck`, `test:coverage` – Vitest, `src/core` 100 % –, `test:e2e` – Playwright with the merged monocart gate over `src` 100 % –, `build:check`, `build` with the SRI/CSP check), E2E orchestrator lint/types/tests (100 %) |
| `make platform-ci-linux` (on the runner device) | `scripts/guest-ci.sh`: the tests marked `linux` (Landlock, systemd units, nftables, guest services) as root in the Lima VM `<prefix>guest-ci`; copies `.coverage.linux.*` back for `coverage combine` |
| `make platform-test-hub`, `make platform-test-runner` | short runs of one part, gated at 100 % for the files of that part |
| `make platform-web`, `make platform-e2e` | the web gates and the E2E orchestrator gates alone |
| `make contracts-version` | regenerates the derived `contracts/VERSION` |

The gates must be green before a change lands. They bind fixed local ports: never run two gate runs at the same time
on one machine with the same port range.

Hygiene: `tests/platform/test_hygiene.py` rejects coverage pragmas, internal host names, private IP addresses, home
paths and token patterns in every platform path; `tests/platform/test_docs.py` checks every link and index of
`docs/platform/`. Both repositories are public: name devices only by role (hub device, runner device, second device).

## 3. E2E (runner device)

```bash
e2e/platform/run.sh --topology t1 [--only E2E-06,E2E-08] [--repeat N] [--project desktop|mobile]
e2e/platform/run.sh --topology t2 --runner <ssh target of the runner device>   # hub on the second device
e2e/platform/run.sh --topology t1 --only PERF-01                               # time to working
e2e/platform/run.sh --topology t1 --live --only LIVE-01                        # live tests, owner accounts only
```

The orchestrator starts the hub, the doubles (model, GitHub, mail, login CLI) and runners with real Lima VMs from a
TEST_MODE golden (ADR-036), runs the Playwright specs `e2e/platform/specs/` with requirement tags and writes the
traceability report. Prerequisites on the runner device: Lima, a golden image or the shared golden cache; runner
and doubles come from the same checkout (`AGENT_RUNNER_SRC` may name another). Every Lima instance a run creates carries the run's prefix
(`AGENT_RUNNER_VM_PREFIX`); delete leftovers of your own prefix only.

Other suites on the runner device: the attack suite `scripts/run-atk.sh --prefix <prefix>` (
[claude-code-upgrade-gate.md](claude-code-upgrade-gate.md)), the monitoring test stack
([monitoring-patch-handover.md](monitoring-patch-handover.md)).

## 4. Conventions

Commits: one English sentence ending with a period, signed. New extension points are discovered, never added to
central lists (ADR-027 §4); schema migrations take the next free number; contract changes are additive with a note
`contracts/changes/<id>.md`. Decisions: `docs/platform/decisions/<id>.md`; architecture decisions: next free ADR.
