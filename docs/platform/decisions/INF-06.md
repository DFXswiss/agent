# INF-06 – decisions (one repository: agent-core merged into agent)

Architecture: [ADR-061](../adr/061-one-repository.md). The merge commit unites the histories and takes agent's side of
the 34 paths that differed; the following commit "Unify the two repositories …" holds every change of content.

1. **Rules for the 34 differing paths.** 19 decision notes become one file each (generated: title, one sentence, the
   agent note, then the agent-core note, headings one level lower; nothing is rewritten). `spec/06-web.md` takes the
   newer side (agent-core, HOT-05; agent's text is contained in it). Root files and tests are merged by hand: see the
   items below. The rule-based part is a script of the switch tooling, so the union can be repeated on later heads.
2. **Base dependencies.** `dfx-agent` keeps agent's base dependencies and appends those of the legacy hub
   (FastAPI, uvicorn, itsdangerous, cbor2): the legacy `Dockerfile` of agent-core builds with `pip install .` and
   stays unchanged. The legacy check allows dependencies only to be appended.
3. **`hub` extra and image.** The hub image installs the `hub` extra (read from `pyproject.toml`) and the distribution
   with `--no-deps`, and copies only `src/agent_hub` and `src/agent_core`. cbor2 and PyYAML belong to the extra because
   the legacy modules the hub imports (passkey, signing, config) reach them; the import-boundary test derives this from
   the source and fails if the extra misses a reached import or names one that is not reached (uvicorn and websockets
   are the server runtime and stay).
4. **`agent_cli.store` for the runner.** The runner's login automation imports `agent_cli.store.StoreError` (the error
   of `newest_magic_link`, as built before INF-06); the import-boundary test allows it beside the modules of
   `00-overview.md` §5.
5. **One coverage gate.** The hub moved from `[tool.coverage]` (pytest-cov) to `.coveragerc-platform` (`coverage run`
   with `patch = subprocess`, as the runner suites); `[tool.coverage]` stays the legacy configuration of `agent_cli`
   and a test pins both. `platform-test-hub` and `platform-test-runner` gate their own files at 100 % for short runs.
6. **Legacy check with two bases.** The second base is the last `develop` commit of agent-core
   (`cbe3dbd8bee1`), pinned in the test: agent-core's `develop` does not move any more and the commit is part of the
   united history (it is an ancestor of agent-core's integration branch). Files both origins had are either united
   with rules (`pyproject.toml`, `.gitignore`) or listed as merged files (README, CONTRIBUTING, `.github/a38.json`,
   the guard and test workflows, the legacy `tests/conftest.py`).
7. **Legacy `tests/conftest.py`.** Both conftests in one file; three adaptations only: `agent_cli.pg` imported as a
   module (both define `create_database`), the per-test database of the agent part skipped for the agent-core test
   modules (listed by name), the agent-core part falling back to `AGENT_TEST_PG` when its own variable is unset (one
   server for CI and the local A38 run), and the test directory on `sys.path` (agent-core's `tests/__init__.py` makes
   it a package, the agent tests import their helpers as top-level modules).
8. **Workflows and A38.** The guard and test workflows keep agent's version; the exclusions of agent-core's image and
   release workflows join `.github/a38.json`, because those workflows now live here. **No delivery from this
   repository:** `develop` of agent is the target branch now, and agent-core's `agent-core-dev.yaml` would publish its
   image and start a DEV deployment on every push to it (`auto-release-pr.yaml` opens release pull requests,
   `agent-core-prd.yaml` delivers on `main`). They are kept (history, later use) but each job gets
   `if: github.repository == 'DFXswiss/agent-core'`, so they never run here; deleting them was the alternative, but a
   guard keeps the legacy files readable and is undone by removing two lines. The legacy check allows exactly these
   two lines. The `platform-*.yml` workflows
   are left as they are: GitHub CI is not part of the binding gates (owner's decision of 2026-11-23).
9. **Copy checks.** Gone: `tests/platform/contracts/test_contracts_copy.py`, `scripts/sync-contracts.sh`, the
   `sync-contracts` target and the sibling comparison of `docs/platform/` in `test_docs.py` (paths named in the
   shared documents must now exist in this tree). The runner's crypto copy stays a copy and gains a test against the
   reference in the same tree.
10. **E2E.** `AGENT_RUNNER_SRC` defaults to the same checkout; the traceability check resolves `agent:` evidence in
    the same checkout. The matrix keeps its `agent-core:`/`agent:` prefixes (changed only by acceptance packages).
11. **Documentation.** Amendments instead of rewrites in the specification (00 §5, §6; 07 §3, §4), ADR-027 and
    CUTOVER; the entry page, the design table, the developer runbook and the web spec paths are updated; historical
    decision and acceptance notes keep their wording ("agent-core" there names the former repository, the path is
    the same).
12. **ADR number.** 057 is the next free number on the integration branch. Running packages may take it first; the
    switch tooling renumbers this ADR to the next free number of the then current heads.
13. **Target branch `develop`.** Since the owner's decision of 2026-11-23 23:40 the target branch of agent is
    `develop` (`ad5f2026-live` follows it as a parallel name). The legacy check still compares with the merge base of
    `origin/develop`: before the switch that base is the legacy-only state, after it the base already contains the
    platform, and the check then means what it says for every later package: platform work does not change legacy
    files (the legacy development on `develop` changes them in its own pull requests, whose suite is the legacy
    `pytest`, which skips `tests/platform`).
