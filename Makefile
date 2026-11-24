# Platform gates (docs/platform/spec/07-testing.md sections 3 and 7, ADR-027 §6, ADR-061). Legacy tests: plain `pytest`.
# The tools come from the active environment (put .venv/bin first in PATH).
#
# Since the repositories were unified (INF-06) one repository carries the hub, the web client, the runner, the guest,
# the doubles, the agent tool, the contracts and the E2E suite. `make platform-ci` checks all of it, with one coverage
# gate of 100 % lines and branches over every platform package together (.coveragerc-platform), plus
# `make platform-ci-linux` for the Linux-only guest code. Short runs of one part: platform-test-hub,
# platform-test-runner (runner, guest, doubles, agent tool), platform-web, platform-e2e.
PYTHON ?= python
COVERAGE = coverage
RC = --rcfile=.coveragerc-platform
PYTEST_ARGS = -c tests/platform/pytest.ini --confcutdir=tests/platform --import-mode=importlib
PY_PATHS = src/agent_hub src/agent_runner src/agent_guest src/agent_doubles src/agent_platform scripts deploy/monitoring/agent_monitoring tests/platform
PLATFORM_CI_STEPS = platform-lint platform-types platform-test platform-contracts platform-web platform-e2e
E2E = e2e/platform
WEB_BIN = ../../web/node_modules/.bin
# Test directories of the two parts; the top-level tests of tests/platform (hygiene, legacy, docs, coverage
# configuration, import boundaries, ...) belong to both.
HUB_TESTS = tests/platform/hub tests/platform/contracts
RUNNER_TESTS = tests/platform/runner tests/platform/guest tests/platform/guest_linux tests/platform/doubles \
	tests/platform/tool tests/platform/images tests/platform/monitoring tests/platform/probes tests/platform/atk
TOP_TESTS = $(sort $(wildcard tests/platform/test_*.py))

.PHONY: platform-ci platform-ci-steps platform-ci-concurrent $(PLATFORM_CI_STEPS) platform-ci-linux \
	platform-test-hub platform-test-runner contracts-version runner-crypto-copy runner-crypto-copy-check

platform-ci: $(PLATFORM_CI_STEPS)

platform-ci-steps:
	@printf '%s\n' $(PLATFORM_CI_STEPS)

# Steps the work-package gates may run beside the others: the Linux part runs in the guest-ci VM while the other steps
# run (platform-test then waits for it through PLATFORM_LINUX_DONE, set by the gates, before it combines the data);
# platform-e2e has its own data files and ports and no browser. The platform tests leave the port range to parallel
# workers only, so one test process (PLATFORM_TEST_WORKERS=0) keeps every step in order.
platform-ci-concurrent:
	@printf '%s\n' platform-ci-linux
	@if [ "$(PLATFORM_TEST_WORKERS)" != 0 ]; then printf '%s\n' platform-e2e; fi

platform-lint:
	ruff check $(PY_PATHS)
	ruff format --check $(PY_PATHS)

platform-types:
	mypy

# Linux-only tests (marker linux) run in platform-ci-linux, live tests never run here.
# Linux coverage data (.coverage.linux.*, fetched from the Linux job or the guest-ci VM) is kept and combined;
# files older than two hours are stale and removed (the work-package gates fetch fresh data before every run).
# Workers of pytest-xdist (0 = one process, auto = one per core); `patch = subprocess` of .coveragerc-platform measures
# every worker, `combine` merges their data with the main process and the Linux part (INF-01). LINUX_WAIT_SECONDS
# bounds the wait for a Linux part that runs beside the tests.
PLATFORM_TEST_WORKERS ?= 4
LINUX_WAIT_SECONDS = 1800

# $(call platform_tests,<test paths>,<extra coverage report options>)
define platform_tests
	rm -f .coverage
	find . -maxdepth 1 -name '.coverage.*' ! -name '.coverage.linux.*' -delete
	find . -maxdepth 1 -name '.coverage.linux.*' -mmin +120 -delete
	$(COVERAGE) run $(RC) -m pytest $(PYTEST_ARGS) -n $(PLATFORM_TEST_WORKERS) -m "not linux and not live" $(1)
	@if [ -n "$$PLATFORM_LINUX_DONE" ]; then n=0; \
		while [ ! -e "$$PLATFORM_LINUX_DONE" ] && [ $$n -lt $(LINUX_WAIT_SECONDS) ]; do sleep 1; n=$$((n + 1)); done; \
		[ -e "$$PLATFORM_LINUX_DONE" ] || echo "platform-test: the Linux part did not end within $(LINUX_WAIT_SECONDS) s"; fi
	$(COVERAGE) combine --keep $(RC) .
	$(COVERAGE) report $(RC) $(2)
endef

platform-test:
	$(call platform_tests,tests/platform,)

# One part only, gated at 100 % for the files of that part (the full gate stays platform-test).
platform-test-hub:
	$(call platform_tests,$(HUB_TESTS) $(TOP_TESTS),--include='*/agent_hub/*')

platform-test-runner:
	$(call platform_tests,$(RUNNER_TESTS) $(TOP_TESTS),--omit='*/agent_hub/*')

platform-contracts:
	$(PYTHON) -m pytest $(PYTEST_ARGS) -q tests/platform/test_contracts_version.py

platform-ci-linux:
	scripts/guest-ci.sh

platform-web:
	cd web && npm run lint && npm run typecheck && npm run test:coverage && npm run test:e2e \
		&& npm run build:check && npm run build

contracts-version:
	$(PYTHON) scripts/contracts_version.py --write

# The runner copy of the reference crypto (decisions/CRY-03.md, HOT-08): write it after a change of
# src/agent_hub/{crypto,log}, src/agent_hub/agents/vectors.py or their tests; the check only reports differing files.
runner-crypto-copy:
	$(PYTHON) tests/platform/runner/crypto/copy_tool.py

runner-crypto-copy-check:
	$(PYTHON) tests/platform/runner/crypto/copy_tool.py --check

# E2E orchestrator and specs (07-testing.md section 5): lint, types, unit tests with 100 % coverage, traceability.
platform-e2e:
	ln -sfn ../../web/node_modules $(E2E)/node_modules
	cd $(E2E) && ruff check . && ruff format --check . && MYPYPATH=.:tests mypy --strict orchestrator perf checks live tests test_traceability.py
	cd $(E2E) && $(WEB_BIN)/eslint --max-warnings 0 . && $(WEB_BIN)/prettier --check . && $(WEB_BIN)/tsc -p tsconfig.json --noEmit
	cd $(E2E) && coverage erase --rcfile=.coveragerc && coverage run --rcfile=.coveragerc -m pytest -c pytest.ini -q \
		&& coverage combine --rcfile=.coveragerc -q && coverage report --rcfile=.coveragerc
