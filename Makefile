# Platform gates (docs/platform/spec/07-testing.md sections 3 and 7, ADR-027 §6). Legacy tests: plain `pytest`.
# The tools come from the active environment (put .venv/bin first in PATH).
PYTHON ?= python
COVERAGE = coverage
RC = --rcfile=.coveragerc-platform
PYTEST_ARGS = -c tests/platform/pytest.ini --confcutdir=tests/platform --import-mode=importlib
PY_PATHS = src/agent_runner src/agent_guest src/agent_doubles tests/platform
PLATFORM_CI_STEPS = platform-lint platform-types platform-test platform-contracts
AGENT_CORE ?= ../agent-core

.PHONY: platform-ci platform-ci-steps $(PLATFORM_CI_STEPS) platform-ci-linux sync-contracts

platform-ci: $(PLATFORM_CI_STEPS)

platform-ci-steps:
	@printf '%s\n' $(PLATFORM_CI_STEPS)

platform-lint:
	ruff check $(PY_PATHS)
	ruff format --check $(PY_PATHS)

platform-types:
	mypy

# Linux coverage data (.coverage.linux.*, fetched from the Linux job or the guest-ci VM) is kept and combined.
platform-test:
	rm -f .coverage
	find . -maxdepth 1 -name '.coverage.*' ! -name '.coverage.linux.*' -delete
	$(COVERAGE) run $(RC) -m pytest $(PYTEST_ARGS) tests/platform
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
