# Platform gates (docs/platform/spec/07-testing.md sections 3 and 7, ADR-027 §6). Legacy tests: plain `pytest`.
# The tools come from the active environment (put .venv/bin first in PATH).
PYTHON ?= python
COVERAGE = coverage
RC = --rcfile=.coveragerc-platform
PYTEST_ARGS = -c tests/platform/pytest.ini --confcutdir=tests/platform --import-mode=importlib
PY_PATHS = src/agent_runner src/agent_guest src/agent_doubles src/agent_platform scripts/probes deploy/monitoring/agent_monitoring tests/platform
PLATFORM_CI_STEPS = platform-lint platform-types platform-test platform-contracts
AGENT_CORE ?= ../agent-core

.PHONY: platform-ci platform-ci-steps platform-ci-concurrent $(PLATFORM_CI_STEPS) platform-ci-linux sync-contracts

platform-ci: $(PLATFORM_CI_STEPS)

platform-ci-steps:
	@printf '%s\n' $(PLATFORM_CI_STEPS)

# Steps the work-package gates may run beside the others: the Linux part runs on the runner device while the local
# steps run; platform-test then waits for it (PLATFORM_LINUX_DONE, set by the gates) before it combines the data.
platform-ci-concurrent:
	@printf '%s\n' platform-ci-linux

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

platform-test:
	rm -f .coverage
	find . -maxdepth 1 -name '.coverage.*' ! -name '.coverage.linux.*' -delete
	find . -maxdepth 1 -name '.coverage.linux.*' -mmin +120 -delete
	$(COVERAGE) run $(RC) -m pytest $(PYTEST_ARGS) -n $(PLATFORM_TEST_WORKERS) -m "not linux and not live" tests/platform
	@if [ -n "$$PLATFORM_LINUX_DONE" ]; then n=0; \
		while [ ! -e "$$PLATFORM_LINUX_DONE" ] && [ $$n -lt $(LINUX_WAIT_SECONDS) ]; do sleep 1; n=$$((n + 1)); done; \
		[ -e "$$PLATFORM_LINUX_DONE" ] || echo "platform-test: the Linux part did not end within $(LINUX_WAIT_SECONDS) s"; fi
	$(COVERAGE) combine --keep $(RC) .
	$(COVERAGE) report $(RC)

platform-contracts:
	$(PYTHON) -m pytest $(PYTEST_ARGS) -q tests/platform/test_contracts_version.py

platform-ci-linux:
	scripts/guest-ci.sh

sync-contracts:
	@if [ -x scripts/sync-contracts.sh ]; then \
		scripts/sync-contracts.sh "$(AGENT_CORE)"; \
	else \
		echo "sync-contracts: scripts/sync-contracts.sh does not exist yet (CTR-01 adds it)" >&2; exit 1; \
	fi
