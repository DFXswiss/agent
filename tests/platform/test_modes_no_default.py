"""No working mode without a choice (E-32, E-19; ADR-098 section 2, KOR-75).

`hub.task_mode` is written only by `agent_hub.modes.store` (`switch_on`, `switch_off`, `store_driver`), a mode is
switched on only by the ways of ADR-098 section 3 - the person routes and task creation with an explicit list
(`modes.choice.choose`), the device route of an executing `task.mode` (`modes.routes.sentence`) - and no migration,
installation, creation or catalogue path writes a row. On the runner the only way to the device route is the
executor of `task.mode`. The behaviour (a task created without `modes` has none, a fresh database has no row) is
covered by `tests/platform/hub/modes/test_routes.py` and `test_no_default_rows` there.
"""

from __future__ import annotations

import re
from pathlib import Path

WRITE_RE = re.compile(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+hub\.task_mode\b", re.IGNORECASE)
SWITCH_ON_RE = re.compile(r"\bswitch_on\(")


def _python(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def test_only_the_store_writes_the_record(repo_root: Path) -> None:
    writers = {
        str(path.relative_to(repo_root))
        for path in _python(repo_root / "src")
        if WRITE_RE.search(path.read_text(encoding="utf-8"))
    }
    assert writers == {"src/agent_hub/modes/store.py"}


def test_no_migration_writes_a_mode(repo_root: Path) -> None:
    for path in sorted((repo_root / "src/agent_hub/db/migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"INSERT\s+INTO\s+hub\.task_mode", text, re.IGNORECASE) is None, path.name


def test_only_the_ways_of_the_adr_switch_a_mode_on(repo_root: Path) -> None:
    callers = {
        str(path.relative_to(repo_root))
        for path in _python(repo_root / "src")
        if SWITCH_ON_RE.search(path.read_text(encoding="utf-8")) and path.name != "store.py"
    }
    assert callers == {"src/agent_hub/modes/choice.py", "src/agent_hub/modes/routes.py"}
    service = (repo_root / "src/agent_hub/tasks/service.py").read_text(encoding="utf-8")
    assert "if modes:" in service  # task creation chooses only the modes the request names


def test_drivers_run_no_repository_code(repo_root: Path) -> None:
    """ADR-098 section 4: a driver starts no process and runs no shell; repository code runs only in test VMs."""
    forbidden = re.compile(r"\bsubprocess\b|create_subprocess|os\.system|os\.exec|os\.spawn|\bpty\b|\bshlex\b")
    for path in _python(repo_root / "src/agent_runner/modes"):
        assert forbidden.search(path.read_text(encoding="utf-8")) is None, path.name


def test_the_runner_reaches_the_device_route_only_from_the_executor(repo_root: Path) -> None:
    users = {
        str(path.relative_to(repo_root))
        for path in _python(repo_root / "src/agent_runner")
        if "/modes/" in path.read_text(encoding="utf-8") and "v1/device/tasks" in path.read_text(encoding="utf-8")
    }
    assert users <= {"src/agent_runner/modes/executors.py", "src/agent_runner/modes/drivers.py"}
