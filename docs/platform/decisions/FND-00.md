# FND-00 decisions (foundation)

Small decisions of the foundation package; they apply to both repositories.

## Layout and packages
1. `docs/platform/` holds `spec/` (copy of the specification 00–10), `adr/` (all ADRs incl. ADR-027),
   `PLATFORM-DESIGN.md` and `decisions/`, in **both** repositories (the agent repository was to hold a runner-facing
   subset per `00-overview.md` §5.2; a full copy is simpler to keep consistent, DOC-01 finalises). References to
   workspace documents (project brief, concept, spikes, host notes) stay as text; those files are not copied.
   No spec text needed neutralising: the copy passes the hygiene test unchanged.
2. Every area package is created as an empty `__init__.py` so parallel packages never add the same file
   (hub: 28 areas; runner: 25; guest: 5 registration directories; doubles: `model, github, mail, login_cli, common`).
   `agent_runner/service` is **not** created: RUN-01 owns the module `agent_runner/service.py`, and a package of the
   same name would collide.
3. `__version__ = "0.1.0"` is a constant in each package `__init__` (equal to the project version) instead of
   `importlib.metadata`: the guest runs inside the VM from a source tree without distribution metadata.
4. The placeholder commands `agent-hub`, `agent-runner`, `agent-guest` support `--version` and print help otherwise;
   HUB-02, RUN-01 and GST-01 replace them. `agent_doubles` has no console script (each double gets
   `python -m agent_doubles.<double>`).

## Dependencies (`pyproject.toml`, new extras only)
5. agent-core `platform` = fastapi, uvicorn[standard], psycopg[binary], psycopg-pool, cryptography ≥ 50, httpx,
   websockets. agent `platform` = the runner stack only (httpx, websockets, psycopg[binary], psycopg-pool,
   cryptography ≥ 50): the runner serves no HTTP framework, guest and doubles are stdlib-only. agent `login` =
   playwright ≥ 1.65.
6. Lower bounds (`>=`) as both repositories do; `ruff==0.17.6` and `mypy==2.4.1` are pinned exactly because
   formatter and type-checker upgrades change results. `platform-test` = pytest, coverage ≥ 7.10.7 (needed for
   `patch = subprocess`), jsonschema, ruff, mypy, types-jsonschema, types-PyYAML.

## Gates
7. `make platform-ci` = the steps printed by `make platform-ci-steps` (agent-core: lint, types, test, contracts,
   web; agent: lint, types, test, contracts). `tools/wp/gates.sh` runs them one by one for its summary.
8. Pytest for the platform suites runs with `-c tests/platform/pytest.ini --confcutdir=tests/platform
   --import-mode=importlib`, so the legacy `tests/conftest.py` (autouse fixtures, legacy clusters) is never loaded;
   the legacy `[tool.pytest.ini_options]` stays as it is.
9. ruff: line length 120, rules E, F, W, I, B, UP, SIM, RUF, PL, C4, PT (PLR2004 and PLR0913 off). mypy: `strict`
   plus `warn_unreachable`, `explicit_package_bases`; paths are listed in `[tool.mypy] files` and in the Makefile,
   never the legacy packages.
10. agent `platform-test` keeps `.coverage.linux.*` (fetched from the Linux job or the guest-ci VM) and combines
    them with `--keep`; every other data file is removed first.

## Contracts
11. `contracts/VERSION` = SHA-256 over the manifest lines `<sha256 of the file>  <relative path>\n` of every file
    under `contracts/` except the top-level `VERSION`, sorted by POSIX path; directories `node_modules`,
    `__pycache__`, `.pytest_cache` and files `.DS_Store` are skipped (git-ignored content must not change the
    hash). The value can be reproduced with standard tools. agent-core computes it in `scripts/contracts_version.py`
    (`make contracts-version`); the agent test recomputes it inline.

## Hygiene test (R-P.9)
12. Coverage and lint exclusions (coverage.py's default exclusion pattern, i.e. `pragma` with or without colon or
    blanks followed by `no cover`/`no branch` in any case, `istanbul ignore`, `v8|c8 ignore`, bare
    `type: ignore` without an error code, bare `noqa` without a rule) are refused in code files; Markdown is
    exempt because the specification quotes the rule.
13. Internal host and account names are matched as SHA-256 digests of lower-cased alphanumeric tokens (also with
    trailing digits stripped), so the public test never spells them. Further checks: `*.local` host names, every
    IPv4 address except loopback, `0.0.0.0`, the RFC 5737 networks and the public attack-suite egress target named in
    07 §2.3 (explicit allow-list), IPv6 unique-local (RFC 4193) and link-local addresses (RFC 3849
    and `::1` pass), home paths of the macOS user directory, subscription profile names, token shapes (GitHub,
    Anthropic, OpenRouter, AWS, PEM private keys, Slack).
14. Scanned: every platform path (packages, `tests/platform`, `docs/platform`, `contracts`, `scripts`, web/e2e
    resp. images/monitoring, `Makefile`, `platform-*.yml`); `node_modules`, virtualenvs, caches and binary files
    are skipped.

## Legacy-untouched test (R-P.2)
15. Compared with the merge base of `origin/develop` incl. staged, unstaged and untracked files. Added files must
    lie in the platform areas; of existing files only `.gitignore` (lines appended) and `pyproject.toml` (new
    tables at the top level or directly under `[tool]`, new keys only in `project.optional-dependencies`,
    `project.scripts`, `tool.setuptools.package-data`) may change; a new key in an existing legacy table (for example
    `omit` in `[tool.coverage.run]`) is refused. A missing `origin/develop` fails the test (CI checks out with `fetch-depth: 0`).

## CI
16. GitHub only shares artifacts between jobs of one workflow run, so the agent `combine` job lives in
    `platform-runner.yml` and calls `platform-guest-linux.yml` as a reusable workflow (`workflow_call`) instead of
    sitting in `platform-guest-linux.yml` (07 §8). The guest workflow has no push/PR trigger of its own (it would
    run twice), only `workflow_call` and `workflow_dispatch`.
17. `platform-web.yml` skips every step with a message while `web/package.json` does not exist. The SRI/CSP check
    of 07 §8 is part of `npm run build`, which WEB-01 creates.

## Interfaces of the work-package tools (`tools/wp/` in the planning workspace)
18. `e2e.sh` runs `e2e/platform/run.sh --topology t1|t2 [--only IDs] [--live] [--repeat N]` (T2 adds
    `--runner <user>@<LAN address of the runner device>`, resolved at run time) from the agent-core checkout on the
    device and exports `WP_PORT_BASE` (20 ports per package), `AGENT_RUNNER_VM_PREFIX`, `AGENT_RUNNER_SRC` and
    `AGENT_CORE_SRC` (both checkouts) and `E2E_ARTIFACTS_DIR` (collected after the run). SCN-00 implements `run.sh`
    against this contract; until it exists `e2e.sh` reports "spec does not exist yet" and succeeds.
19. `golden-cache.sh <ID> ensure` names the cache `cache-golden-<sha12>` (sha = the manifest hash of rule 11 over
    `images/` and `src/agent_guest/`) and builds it with
    `AGENT_RUNNER_VM_PREFIX=cache- agent-runner image build --tag <sha12>`; VM-01 implements `image build --tag`
    so that the resulting golden is named `<prefix>golden-<tag>`.
20. `gates.sh` runs the steps of `make platform-ci-steps` one by one; in agent it first runs
    `make platform-ci-linux` on the runner device as soon as `tests/platform/guest_linux/` holds tests and copies
    `.coverage.linux.*` back before `platform-test` combines them (rule 10).
21. Packages working in both repositories share one port range and one local PostgreSQL cluster; `testpg.sh`
    reuses the cluster that already runs in the other worktree of the same package.
22. `merge.sh` regenerates `contracts/VERSION` (conflicts during the rebase and packages that changed contracts) and
    refuses duplicate numbers of migrations (`src/**/migrations/NNN_*`) and ADRs (`docs/platform/adr/NNN-*.md`).

## Review follow-ups
23. The legacy run (plain `pytest`, `testpaths = ["tests"]`) must never collect `tests/platform` (legacy autouse
    fixtures, no platform extras, shallow CI checkout without `origin/develop`). `tests/platform/conftest.py`
    implements `pytest_ignore_collect` and ignores the directory unless the run uses `tests/platform/pytest.ini`
    (verified with pytest 9.2; `test_legacy_pytest_run_skips_the_platform_suites` checks it). The legacy
    `tests/conftest.py` stays untouched.
24. `make platform-test` deselects the markers `linux` (agent; run by `platform-ci-linux`) and `live` (both repos;
    never part of `platform-ci`).
25. `test_coverage_config.py` pins the coverage configuration exactly to 07 §3, so a later `omit` or a lower
    `fail_under` fails the gate.
26. agent `platform-test` removes `.coverage.linux.*` files older than two hours before combining; the work-package
    gates fetch fresh Linux data before every run.
27. All platform workflows declare `permissions: contents: read`. Each repository keeps the action versions of its
    existing workflows.
28. `contracts/README.md` is identical in both repositories, so the agent copy is byte-identical to agent-core's
    `contracts/` and both `VERSION` values match (CON-04).
