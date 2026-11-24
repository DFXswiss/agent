# ADR-061: One repository – agent-core is merged into agent; one distribution with separate extras

Status: Accepted (2026-11-23)

## Context
- The platform was built in two repositories (ADR-001, `00-overview.md` §5): hub, web client, contracts and the E2E
  suite in `agent-core`, runner, guest, doubles, images and the agent tool in `agent`. Both carried the same
  `contracts/` (3 283 files, copied by a merge script) and the same `docs/platform/spec/` and `adr/`; of 3 526 paths
  present in both, 3 492 were byte-identical and 34 differed (decision notes of packages that changed both sides,
  one spec file, the shared root files and tests).
- Nearly every package needed two branches, two pull requests, two gate runs and a comparison of the copies; numbers
  were given out twice and a copy drifted on the integration branch.
- The owner decided on 2026-11-23: "repos zusammenlegen, nur noch /agent".
- Constraints: the history of both repositories stays readable (`git log`, `git blame`); no path changes (the
  specification, ADRs, decisions, tools and running packages name the paths); `uv tool install
  "dfx-agent[platform] @ git+…/agent@<ref>"` keeps delivering the agent tool; the hub image installs only what the
  hub needs; runner, guest and hub do not import each other; the legacy areas (`agent_cli`, `agent_core`, their
  deploy files, `*-PROTOCOL.md`) stay unchanged.

## Decision

### 1. History and layout
- `agent-core` is merged into `agent` with `git merge --allow-unrelated-histories`; the first parent is the head of
  agent's `develop`, which is the target branch since the owner's decision of 2026-11-23 23:40 (`ad5f2026-live`
  points to the same commit as a parallel name until every running package has moved), so both move on by
  fast-forward. The other branches and tags of
  agent-core are kept under the name space `core/` (branches `core/<name>`, tags `core/<tag>`).
- Every file keeps its path (union at the root). The merge commit takes agent's side of the 34 differing files; one
  following commit unites them and adapts the platform areas, so the union of the histories and the change of
  content can be read separately.
- Rules for the differing files: a decision note `docs/platform/decisions/<ID>.md` becomes one file with both notes
  (agent part first); other `docs/platform/` files take the newer side; root files and tests are merged by hand
  (pyproject, Makefile, `.gitignore`, README, CONTRIBUTING, `.github/a38.json`, legacy `tests/conftest.py`, the
  top-level platform tests). One `contracts/`, one `contracts/VERSION`, one `docs/platform/`.

### 2. Packaging: one distribution with separate extras
- `pyproject.toml` stays the distribution `dfx-agent` (`src/` layout, every package of `src/`). Its base
  dependencies are those of `agent_cli` followed by those of the legacy hub `agent_core`, because the legacy
  `Dockerfile` of agent-core builds with `pip install .` and must keep working unchanged.
- Extras: `platform` (runner and agent tool, unchanged), **`hub`** (new: FastAPI, uvicorn, psycopg with pool,
  cryptography ≥ 50, httpx, websockets, and cbor2/PyYAML for the legacy modules the hub imports), `platform-test`
  (union of both repositories), `login`, `test`.
- Console scripts of both: `agent`, `agent-runner`, `agent-guest`, `agent-platform`, `agent-hub`, `agent-core`.
- **Hub image**: the build copies only `src/agent_hub` and `src/agent_core` (plus `pyproject.toml`, README,
  LICENSE) into the image stage, installs the `hub` extra read from `pyproject.toml` and then the distribution with
  `--no-deps`; it checks that `agent_runner`, `agent_guest`, `agent_doubles`, `agent_platform` and `agent_cli` cannot
  be imported, and removes the console scripts of the other packages (`pip install --no-deps .` creates all six; only
  `agent-hub` can run there). No runner, guest or legacy CLI code is in the image.
- **Execution device**: `uv tool install "dfx-agent[platform] @ git+…/agent@<ref>"` installs the whole distribution,
  as before plus the hub and legacy hub code and their few base dependencies (FastAPI, uvicorn, itsdangerous,
  cbor2). That code is public, holds no secrets and is never started on the device (no service, no listener); the
  runner's own code path does not import it (test below).
- **Import boundaries as a test** (`tests/platform/test_import_boundaries.py`, on the source, including imports inside
  functions): the hub imports nothing of runner, guest, doubles, agent tool or legacy CLI and only the legacy
  modules of `00-overview.md` §5; runner and agent tool import nothing of the hub or the legacy hub; guest and
  doubles use the standard library only; every module the hub reaches lies in `agent_hub`/`agent_core` and every
  third-party import it reaches is covered by the `hub` extra.

### 3. Gates
- Same target names. `make platform-ci` = lint, types, `platform-test` (hub, runner, guest, doubles and agent tool in
  one run with **one** coverage gate of 100 % lines and branches, `.coveragerc-platform`), contracts, web, E2E
  orchestrator; `make platform-ci-linux` for the Linux-only guest code (combined into the same report). Short runs:
  `platform-test-hub`, `platform-test-runner`, `platform-web`, `platform-e2e`.
- Checks that only secured the equality of the two copies are gone: the contract copy test and
  `scripts/sync-contracts.sh`, the comparison of `docs/platform/` with the sibling checkout. The crypto copy of the
  runner (ADR-027 §2) stays a copy (the runner must not import the hub) and is now checked against the reference in
  the same tree. The legacy check knows both legacy bases (agent's `develop` and the last `develop` of agent-core).
- The E2E orchestrator takes the runner and doubles from the same checkout unless `AGENT_RUNNER_SRC` names another.

### 4. Release
- The hub image is built from a commit of `DFXswiss/agent` (stack template: repository address, copied packages,
  `hub` extra); the reproducible web build and the build hash stay as they are and belong to exactly one commit of
  `DFXswiss/agent`. Runner installation, golden image and agent tool come from the same commit.

## Consequences
- One branch, one gate run and one review per package; contract changes and their implementations land together.
- Gate runs cover all parts, so they are longer; the part targets keep short runs possible.
- A change of the hub's reference crypto fails the runner's copy test in the same run until the copy is renewed
  (before, the drift showed only at the next synchronisation).
- The legacy workflows of agent-core that deliver (`agent-core-dev.yaml`: image and DEV deployment on every push to
  `develop`; `agent-core-prd.yaml`: the same for `main`; `auto-release-pr.yaml`: a release pull request after every
  push to `develop`) come along unchanged but switched off: each job runs only in the former repository
  (`if: github.repository == 'DFXswiss/agent-core'`). A push to `develop` of this repository delivers nothing; the
  owner can switch them on later (remove the condition, add the secrets, extend the receiver's allow-list).
- The repository `agent-core` is archived after the switch (repository administrator).

## Alternatives considered
- **Two distributions without moving directories** (a second `pyproject.toml` for the hub): setuptools does not build
  packages from outside the project directory without moving `src/agent_hub` or a custom build backend, and the
  legacy Dockerfile of agent-core needs `pip install .` at the root to yield `agent_core`. Rejected.
- **Subdirectories per former repository** (`hub/`, `runner/`): changes every path named in specification, ADRs,
  decisions, tools and running packages; rejected by the owner's frame ("no path changes").
- **Keeping two repositories with better tooling**: the cost (double branches, copies, numbering) is structural.

## Deviation from DESIGN.md
None beyond ADR-001; `00-overview.md` §5 and §6 (two repositories) are superseded by this ADR.
