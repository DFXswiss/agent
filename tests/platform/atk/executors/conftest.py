"""Fixtures of ATK-33 against the real executors (ATS-03): the runner's store and GitHub-double fixtures (RUN-01,
RUN-09) reused as they are."""

from tests.platform.runner.conftest import runner_dsn, runner_pg_admin  # noqa: F401 (fixtures)
from tests.platform.runner.github.conftest import start_github, store_dsn  # noqa: F401 (fixtures)
