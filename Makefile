# Platform gates (docs/platform/spec/07-testing.md sections 3 and 7, ADR-027 §6). Legacy tests: plain `pytest`.
# The tools come from the active environment (put .venv/bin first in PATH).
PYTHON ?= python
PYTEST_ARGS = -c tests/platform/pytest.ini --confcutdir=tests/platform --import-mode=importlib
PY_PATHS = src/agent_hub tests/platform scripts
PLATFORM_CI_STEPS = platform-lint platform-types platform-test platform-contracts platform-web platform-e2e
E2E = e2e/platform
# Workers of pytest-xdist in platform-test (0 = one process, auto = one per core); pytest-cov measures every worker with
# the [tool.coverage] configuration of pyproject.toml and combines the data before the report (INF-01).
PLATFORM_TEST_WORKERS ?= 4
WEB_BIN = ../../web/node_modules/.bin

.PHONY: platform-ci platform-ci-steps platform-ci-concurrent $(PLATFORM_CI_STEPS) contracts-version

platform-ci: $(PLATFORM_CI_STEPS)

platform-ci-steps:
	@printf '%s\n' $(PLATFORM_CI_STEPS)

# Steps the work-package gates may run beside the others: own data files, ports and no browser (the browser suites of
# platform-web keep the machine to themselves). The platform tests leave the port range to parallel workers only, so
# one test process keeps every step in order.
platform-ci-concurrent:
	@if [ "$(PLATFORM_TEST_WORKERS)" != 0 ]; then printf '%s\n' platform-e2e; fi

platform-lint:
	ruff check $(PY_PATHS)
	ruff format --check $(PY_PATHS)

platform-types:
	mypy

platform-test:
	coverage erase
	$(PYTHON) -m pytest $(PYTEST_ARGS) -n $(PLATFORM_TEST_WORKERS) --cov --cov-report= -m "not live" tests/platform
	coverage report

platform-contracts:
	$(PYTHON) -m pytest $(PYTEST_ARGS) -q tests/platform/test_contracts_version.py

platform-web:
	cd web && npm run lint && npm run typecheck && npm run test:coverage && npm run test:e2e \
		&& npm run build:check && npm run build

contracts-version:
	$(PYTHON) scripts/contracts_version.py --write

# E2E orchestrator and specs (07-testing.md section 5): lint, types, unit tests with 100 % coverage, traceability.
platform-e2e:
	ln -sfn ../../web/node_modules $(E2E)/node_modules
	cd $(E2E) && ruff check . && ruff format --check . && MYPYPATH=.:tests mypy --strict orchestrator perf checks live tests test_traceability.py
	cd $(E2E) && $(WEB_BIN)/eslint --max-warnings 0 . && $(WEB_BIN)/prettier --check . && $(WEB_BIN)/tsc -p tsconfig.json --noEmit
	cd $(E2E) && coverage erase --rcfile=.coveragerc && coverage run --rcfile=.coveragerc -m pytest -c pytest.ini -q \
		&& coverage combine --rcfile=.coveragerc -q && coverage report --rcfile=.coveragerc
