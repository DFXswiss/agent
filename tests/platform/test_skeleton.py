"""Tests of the package skeleton (FND-00, HUB-02): every area package imports, `agent-hub --version` works."""

from __future__ import annotations

import importlib
import pkgutil
import shutil
import subprocess
import sys

import pytest

import agent_hub
from agent_hub.__main__ import main

AREAS = [
    "agents",
    "audit",
    "auth",
    "backup",
    "config_api",
    "credentials",
    "crypto",
    "db",
    "device",
    "events",
    "health",
    "http",
    "invites",
    "jobs",
    "log",
    "me",
    "monitoring",
    "notifications",
    "operations",
    "orgs",
    "outbox",
    "platform",
    "requests",
    "scheduler",
    "sessions",
    "stream",
    "tasks",
    "usage",
    "whiteboards",
    "workgroups",
    "wraps",
]


@pytest.mark.parametrize("area", AREAS)
def test_area_package_imports(area: str) -> None:
    module = importlib.import_module(f"agent_hub.{area}")
    assert module.__name__ == f"agent_hub.{area}"


def test_every_area_package_is_listed() -> None:
    found = sorted(info.name for info in pkgutil.iter_modules(agent_hub.__path__) if info.ispkg)
    assert found == sorted(AREAS)


def test_version_in_process(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out == f"agent-hub {agent_hub.__version__}\n"


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--help"])
    assert stop.value.code == 0
    assert "usage: agent-hub" in capsys.readouterr().out


def test_console_script() -> None:
    script = shutil.which("agent-hub")
    if script is None:
        pytest.fail("console script agent-hub is not installed (pip install -e '.[platform,platform-test]')")
    done = subprocess.run([script, "--version"], capture_output=True, text=True, check=True)
    assert done.stdout == f"agent-hub {agent_hub.__version__}\n"


def test_module_entry_point() -> None:
    done = subprocess.run([sys.executable, "-m", "agent_hub", "--version"], capture_output=True, text=True, check=True)
    assert done.stdout.startswith("agent-hub ")
