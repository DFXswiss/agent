"""Placeholder tests of the package skeletons (FND-00): every area package imports, the console scripts work."""

from __future__ import annotations

import importlib
import pkgutil
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from types import ModuleType

import pytest

import agent_doubles
import agent_guest
import agent_runner
from agent_guest.__main__ import main as guest_main
from agent_runner.__main__ import main as runner_main

AREAS: dict[ModuleType, list[str]] = {
    agent_runner: [
        "assignments",
        "catchup",
        "credentials",
        "crypto",
        "doctor",
        "enroll",
        "github",
        "guestlink",
        "inbox",
        "login",
        "metrics",
        "model",
        "operations",
        "orgs",
        "pings",
        "prompts",
        "queries",
        "quota",
        "reconcile",
        "requests",
        "sessions",
        "store",
        "subagents",
        "sync",
        "vm",
    ],
    agent_guest: ["api_routes", "control_routes", "hook_addons", "mcp_tools", "sandbox"],
    agent_doubles: ["common", "github", "login_cli", "mail", "model"],
}
COMMANDS: dict[str, tuple[Callable[[Sequence[str] | None], int], str]] = {
    "agent-runner": (runner_main, agent_runner.__version__),
    "agent-guest": (guest_main, agent_guest.__version__),
}


@pytest.mark.parametrize(("package", "area"), [(package, area) for package, areas in AREAS.items() for area in areas])
def test_area_package_imports(package: ModuleType, area: str) -> None:
    module = importlib.import_module(f"{package.__name__}.{area}")
    assert module.__name__ == f"{package.__name__}.{area}"


@pytest.mark.parametrize("package", list(AREAS))
def test_every_area_package_is_listed(package: ModuleType) -> None:
    found = sorted(info.name for info in pkgutil.iter_modules(package.__path__) if info.ispkg)
    assert found == AREAS[package]


@pytest.mark.parametrize("name", list(COMMANDS))
def test_version_in_process(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    main, version = COMMANDS[name]
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out == f"{name} {version}\n"


@pytest.mark.parametrize("name", list(COMMANDS))
def test_help_without_arguments(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    main, _ = COMMANDS[name]
    assert main([]) == 0
    assert f"usage: {name}" in capsys.readouterr().out


@pytest.mark.parametrize("name", list(COMMANDS))
def test_console_script(name: str) -> None:
    script = shutil.which(name)
    if script is None:
        pytest.fail(f"console script {name} is not installed (pip install -e '.[platform,platform-test]')")
    done = subprocess.run([script, "--version"], capture_output=True, text=True, check=True)
    assert done.stdout == f"{name} {COMMANDS[name][1]}\n"


@pytest.mark.parametrize(("module", "name"), [("agent_runner", "agent-runner"), ("agent_guest", "agent-guest")])
def test_module_entry_point(module: str, name: str) -> None:
    done = subprocess.run([sys.executable, "-m", module, "--version"], capture_output=True, text=True, check=True)
    assert done.stdout.startswith(f"{name} ")
