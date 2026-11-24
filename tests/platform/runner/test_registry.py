"""U-RUN registry mechanics (ADR-027 section 4): discovery of tasks, hooks, steps, handlers, cleaners and CLI."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_runner import registry
from agent_runner.errors import RunnerError

TASKS_OK = """
from agent_runner.registry import TaskSpec

async def run(context):
    return None

TASKS = [TaskSpec("{name}", run)]
"""

STEPS = """
from agent_runner.registry import ReconcileStep

async def run(context):
    return None

STEPS = [ReconcileStep({order}, "{name}", run)]
"""


def test_real_package_registries() -> None:
    # Areas register as they land; every lookup of the real package must succeed (no half registry). The sync
    # area's own registrations are checked in tests/platform/runner/sync/test_tasks.py.
    assert {spec.name for spec in registry.tasks()} >= {"sync", "ws", "vm-pool", "vm-idle", "billing_meter"}
    assert {hook.name for hook in registry.shutdown_hooks()} >= {"outbox-flush"}
    assert isinstance(registry.reconcile_steps(), list)
    assert "vm-instances" in [cleaner.name for cleaner in registry.cleaners()]
    assert isinstance(registry.journal_handlers(), dict)
    assert isinstance(registry.executors(), dict)
    assert isinstance(registry.request_handlers(), dict)
    assert "hello" in registry.hint_handlers()
    # Areas register commands as they land (vm: `image`); every registration must be a callable.
    assert all(callable(register) for _area, register in registry.cli_registrars())
    assert "store" in registry.areas()
    assert "sync" in registry.areas()
    # The VM area's own provider holds nothing (its VMs are counted from `runner.vm`).
    assert len(registry.capacity_usages()) >= 1


def test_tasks_sorted_by_area(area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package(
        {
            "zeta/tasks.py": TASKS_OK.format(name="zeta-task"),
            "alpha/tasks.py": TASKS_OK.format(name="alpha-task"),
            "beta/other.py": "X = 1\n",
        }
    )
    assert registry.areas(pkg) == ["alpha", "beta", "zeta"]
    assert [spec.name for spec in registry.tasks(pkg)] == ["alpha-task", "zeta-task"]


def test_duplicate_task_names(area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package({"a/tasks.py": TASKS_OK.format(name="sync"), "b/tasks.py": TASKS_OK.format(name="sync")})
    with pytest.raises(RunnerError) as caught:
        registry.tasks(pkg)
    assert (caught.value.code, caught.value.name) == ("registry_duplicate", "sync")


@pytest.mark.parametrize(
    "source",
    [
        "X = 1\n",
        "TASKS = 'sync'\n",
        "TASKS = 5\n",
        "TASKS = [object()]\n",
        TASKS_OK.format(name="Bad Name"),
    ],
)
def test_invalid_task_modules(source: str, area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package({"area/tasks.py": source})
    with pytest.raises(RunnerError) as caught:
        registry.tasks(pkg)
    assert (caught.value.code, caught.value.name) == ("registry_invalid", "area.tasks")


def test_import_error_inside_a_module_propagates(area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package({"area/tasks.py": "import module_that_does_not_exist_anywhere\n"})
    with pytest.raises(ModuleNotFoundError):
        registry.tasks(pkg)


def test_reconcile_steps_ordered(area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package(
        {
            "a/reconcile.py": STEPS.format(order=30, name="wraps"),
            "b/reconcile.py": STEPS.format(order=10, name="state"),
            "c/reconcile.py": STEPS.format(order=30, name="assignments"),
        }
    )
    assert [(s.order, s.name) for s in registry.reconcile_steps(pkg)] == [
        (10, "state"),
        (30, "assignments"),
        (30, "wraps"),
    ]


def test_shutdown_hooks_and_cleaners(area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package(
        {
            "sync/shutdown.py": (
                "from agent_runner.registry import ShutdownHook\n"
                "async def flush(context):\n    return None\n"
                "HOOKS = (ShutdownHook('outbox-flush', flush),)\n"
            ),
            "vm/uninstall.py": (
                "from agent_runner.registry import Cleaner\n"
                "def delete(context):\n    return None\n"
                "CLEANERS = [Cleaner('vms', delete)]\n"
            ),
        }
    )
    assert [h.name for h in registry.shutdown_hooks(pkg)] == ["outbox-flush"]
    assert [c.name for c in registry.cleaners(pkg)] == ["vms"]


HANDLER_MODULES = [
    ("journal", "HANDLERS", registry.journal_handlers),
    ("executors", "EXECUTORS", registry.executors),
    ("requests", "HANDLERS", registry.request_handlers),
    ("hints", "HANDLERS", registry.hint_handlers),
]


@pytest.mark.parametrize(("module", "attribute", "accessor"), HANDLER_MODULES)
def test_handlers_merge(
    module: str,
    attribute: str,
    accessor: Callable[[str], dict[str, object]],
    area_package: Callable[[dict[str, str]], str],
) -> None:
    pkg = area_package(
        {
            f"a/{module}.py": f"def h(*args):\n    return 'a'\n{attribute} = {{'session.state': h}}\n",
            f"b/{module}.py": f"def h(*args):\n    return 'b'\n{attribute} = {{'usage': h, 'github.pr': h}}\n",
        }
    )
    handlers = accessor(pkg)
    assert sorted(handlers) == ["github.pr", "session.state", "usage"]
    assert handlers["usage"]() == "b"  # type: ignore[operator]


@pytest.mark.parametrize(("module", "attribute", "accessor"), HANDLER_MODULES)
def test_handlers_duplicate(
    module: str,
    attribute: str,
    accessor: Callable[[str], dict[str, object]],
    area_package: Callable[[dict[str, str]], str],
) -> None:
    source = f"def h(*args):\n    return None\n{attribute} = {{'usage': h}}\n"
    pkg = area_package({f"a/{module}.py": source, f"b/{module}.py": source})
    with pytest.raises(RunnerError) as caught:
        accessor(pkg)
    assert (caught.value.code, caught.value.name) == ("registry_duplicate", "usage")


@pytest.mark.parametrize(
    "value",
    ["[]", "{'usage': 5}", "{5: print}", "{'Bad Key': print}"],
)
def test_handlers_invalid(value: str, area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package({"area/journal.py": f"HANDLERS = {value}\n"})
    with pytest.raises(RunnerError) as caught:
        registry.journal_handlers(pkg)
    assert (caught.value.code, caught.value.name) == ("registry_invalid", "area.journal")


def test_cli_registrars(area_package: Callable[[dict[str, str]], str]) -> None:
    pkg = area_package({"doctor/cli.py": "def register(subparsers):\n    return None\n"})
    found = registry.cli_registrars(pkg)
    assert [area for area, _ in found] == ["doctor"]
    bad = area_package({"doctor/cli.py": "register = 5\n"})
    with pytest.raises(RunnerError) as caught:
        registry.cli_registrars(bad)
    assert (caught.value.code, caught.value.name) == ("registry_invalid", "doctor.cli")


def test_capacity_usages(area_package: Callable[[dict[str, str]], str]) -> None:
    source = "async def USAGE(context):\n    return context\n"
    pkg = area_package({"b/capacity.py": source, "a/capacity.py": source, "c/other.py": "X = 1\n"})
    assert len(registry.capacity_usages(pkg)) == 2
    bad = area_package({"a/capacity.py": "USAGE = 5\n"})
    with pytest.raises(RunnerError) as caught:
        registry.capacity_usages(bad)
    assert (caught.value.code, caught.value.name) == ("registry_invalid", "a.capacity")


def test_runner_error_shape() -> None:
    error = RunnerError("config_invalid", "HUB_URL")
    assert str(error) == "config_invalid HUB_URL"
    assert str(RunnerError("unsupported_os")) == "unsupported_os"
    with pytest.raises(ValueError, match="snake"):
        RunnerError("Bad Code")
    with pytest.raises(ValueError, match="identifier"):
        RunnerError("x", "has space = value")
