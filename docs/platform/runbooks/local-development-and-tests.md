# Runbook: local development and tests

Scope: setting up both checkouts, the binding local gates and the cross-repository E2E suite
([07 §1, §3, §5, §7](../spec/07-testing.md#3-coverage-gates), ADR-027 §6). GitHub workflows `platform-*.yml`
run the same commands; when GitHub Actions are unavailable the local gates are binding.

## 1. Setup

Both repositories side by side (`agent-core` and `agent`), each on the integration branch or a branch of it.

```bash
# in each checkout
uv venv --python 3.12 .venv && . .venv/bin/activate
pip install -e ".[platform-test]"          # agent: also gives agent-runner, agent_guest, agent_doubles
cd web && npm ci && cd ..                  # agent-core only (Node version in web/.nvmrc)
```

Tests need a real PostgreSQL ≥ 16 (no mocks, never skipped):

| Repository | Variable | Content |
|---|---|---|
| agent-core | `AGENT_HUB_TEST_POSTGRES` | superuser DSN of a disposable local cluster; each test database `t_hub_<hex>` with owner/writer roles is created by the fixture |
| agent | `AGENT_RUNNER_TEST_PG` | DSN of a disposable cluster (else `initdb` per test session) |

In agent-core a missing variable fails the suite (`pytest.fail`); agent starts its own cluster with `initdb` instead. Use only loopback clusters you own.

## 2. Gates

| Command | Repository | What it runs |
|---|---|---|
| `make platform-ci` | agent-core | `ruff check` + `ruff format --check`, `mypy --strict`, pytest with 100 % line and branch coverage of `agent_hub`, contract version check, web gates (`npm run lint`, `typecheck`, `test:coverage` – Vitest, `src/core` 100 % –, `test:e2e` – Playwright with the merged monocart gate over `src` 100 % –, `build:check`, `build` with the SRI/CSP check), E2E orchestrator lint/types/tests (100 %) |
| `make platform-ci` | agent | ruff, mypy, pytest of runner, guest, doubles, probes library and monitoring helpers with `.coveragerc-platform` (combined with the Linux part), contract hash check |
| `make platform-ci-linux` | agent (on the runner device) | `scripts/guest-ci.sh`: the tests marked `linux` (Landlock, systemd units, nftables, guest services) as root in the Lima VM `<prefix>guest-ci`; copies `.coverage.linux.*` back for `coverage combine` |
| `make sync-contracts` | agent | copies `contracts/` from the sibling agent-core checkout (never edit `agent/contracts/` by hand, ADR-027 §3) |

Both repositories' gates must be green before a change lands. They bind fixed local ports: never run the gates of
the two repositories at the same time on one machine with the same port range.

Hygiene: `tests/platform/test_hygiene.py` rejects coverage pragmas, internal host names, private IP addresses, home
paths and token patterns in every platform path; `tests/platform/test_docs.py` checks every link and index of
`docs/platform/`. Both repositories are public: name devices only by role (hub device, runner device, second device).

## 3. Cross-repository E2E (agent-core, runner device)

```bash
e2e/platform/run.sh --topology t1 [--only E2E-06,E2E-08] [--repeat N] [--project desktop|mobile]
e2e/platform/run.sh --topology t2 --runner <ssh target of the runner device>   # hub on the second device
e2e/platform/run.sh --topology t1 --only PERF-01                               # time to working
e2e/platform/run.sh --topology t1 --live --only LIVE-01                        # live tests, owner accounts only
```

The orchestrator starts the hub, the doubles (model, GitHub, mail, login CLI) and runners with real Lima VMs from a
TEST_MODE golden (ADR-036), runs the Playwright specs `e2e/platform/specs/` with requirement tags and writes the
traceability report. Prerequisites on the runner device: Lima, a golden image or the shared golden cache, the agent
checkout next to agent-core. Every Lima instance a run creates carries the run's prefix
(`AGENT_RUNNER_VM_PREFIX`); delete leftovers of your own prefix only.

Other suites on the runner device: the attack suite `scripts/run-atk.sh --prefix <prefix>` (agent,
[claude-code-upgrade-gate.md](claude-code-upgrade-gate.md)), the monitoring test stack
([monitoring-patch-handover.md](monitoring-patch-handover.md)).

## 4. Conventions

Commits: one English sentence ending with a period, signed. New extension points are discovered, never added to
central lists (ADR-027 §4); schema migrations take the next free number; contract changes are additive with a note
`contracts/changes/<id>.md`. Decisions: `docs/platform/decisions/<id>.md`; architecture decisions: next free ADR.
