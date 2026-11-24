"""U-RUN-install-daemons: the two LaunchDaemons of an own device (ADR-074 sections 3 and 6): plist contents, atomic
0644 writes, `launchctl` states, disabled services, bootout/bootstrap with their closed failure codes, removal."""

from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from agent_runner.install import daemons
from agent_runner.install.layout import MAINTENANCE_LABEL, RUNNER_LABEL

from .conftest import Kit, owner_of, refused

LAUNCHCTL = "/bin/launchctl"
PRINT_RUNNER = [LAUNCHCTL, "print", f"system/{RUNNER_LABEL}"]
PRINT_MAINTENANCE = [LAUNCHCTL, "print", f"system/{MAINTENANCE_LABEL}"]


def test_the_runner_plist(kit: Kit) -> None:
    layout = kit.layout
    payload = layout.root / "Library/Application Support/DFXAgentRunner"
    log = layout.root / "Users/alice/.local/share/dfx-agent-runner/log"
    owner = owner_of(layout)
    assert daemons.runner_plist(kit.system, owner, {"AGENT_RUNNER_HOME": "/h", "PATH": "/override"}) == {
        "Label": "swiss.dfx.agent-runner",
        "ProgramArguments": [str(payload / "current/libexec/dfx-agent-runner"), "agent_runner", "run"],
        "UserName": "alice",
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"PATH": "/override", "AGENT_RUNNER_HOME": "/h"},
        "StandardOutPath": str(log / "runner.out.log"),
        "StandardErrorPath": str(log / "runner.err.log"),
    }
    env = daemons.runner_plist(kit.system, owner, {})["EnvironmentVariables"]
    assert env == {
        "PATH": ":".join(
            [
                str(payload / "current/lima/bin"),
                str(payload / "current/postgresql/bin"),
                str(payload / "host/bin"),
                "/usr/bin:/bin:/usr/sbin:/sbin",
            ]
        )
    }


def test_the_maintenance_plist(kit: Kit) -> None:
    payload = kit.layout.root / "Library/Application Support/DFXAgentRunner"
    assert daemons.maintenance_plist(kit.system) == {
        "Label": "swiss.dfx.agent-runner.maintenance",
        "ProgramArguments": [
            str(payload / "current/libexec/dfx-agent-runner"),
            "agent_runner.install.root",
            "maintain",
        ],
        "RunAtLoad": True,
        "StartInterval": 900,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(payload / "log/maintenance.log"),
        "StandardErrorPath": str(payload / "log/maintenance.log"),
    }
    assert "UserName" not in daemons.maintenance_plist(kit.system)


def test_a_plist_is_written_atomically_with_mode_0644(tmp_path: Path) -> None:
    path = tmp_path / "LaunchDaemons/x.plist"
    daemons.write_plist(path, {"Label": "x", "B": [1]})
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert plistlib.loads(path.read_bytes()) == {"Label": "x", "B": [1]}
    daemons.write_plist(path, {"Label": "y"})
    assert plistlib.loads(path.read_bytes()) == {"Label": "y"}
    assert [item.name for item in path.parent.iterdir()] == ["x.plist"]


def test_loaded_and_state(kit: Kit) -> None:
    kit.run.answer(PRINT_RUNNER, stdout="system/swiss.dfx.agent-runner = {\n\tactive count = 1\n\tstate = running\n}\n")
    kit.run.answer(PRINT_MAINTENANCE, rc=113, stderr="Could not find service")
    assert daemons.loaded(kit.system, RUNNER_LABEL) is True
    assert daemons.loaded(kit.system, MAINTENANCE_LABEL) is False
    assert daemons.state(kit.system, RUNNER_LABEL) == "running"
    assert daemons.state(kit.system, MAINTENANCE_LABEL) == "absent"
    kit.run.answer(PRINT_RUNNER, stdout="system/swiss.dfx.agent-runner = {\n\tlast exit code = 0\n}\n")
    assert daemons.state(kit.system, RUNNER_LABEL) == "unknown"
    kit.run.answer(PRINT_RUNNER, stdout="\t\tstate = not running\n\tstate = waiting\n")
    assert daemons.state(kit.system, RUNNER_LABEL) == "waiting"


@pytest.mark.parametrize(
    ("line", "off"),
    [
        (f'\t"{RUNNER_LABEL}" => disabled', True),
        (f'\t"{RUNNER_LABEL}" => true', True),
        (f'\t"{RUNNER_LABEL}" => enabled', False),
        (f'\t"{RUNNER_LABEL}" => false', False),
        (f'\t"{MAINTENANCE_LABEL}" => disabled', False),
        ('\t"com.other" => disabled', False),
    ],
)
def test_disabled_services(kit: Kit, line: str, off: bool) -> None:
    kit.run.answer([LAUNCHCTL, "print-disabled", "system"], stdout=f"disabled services = {{\n{line}\n}}\n")
    assert daemons.disabled(kit.system, RUNNER_LABEL) is off
    assert kit.run.calls == [[LAUNCHCTL, "print-disabled", "system"]]


def test_bootout_tolerates_an_unloaded_service(kit: Kit) -> None:
    daemons.bootout(kit.system, RUNNER_LABEL)
    kit.run.answer([LAUNCHCTL, "bootout"], rc=3, stderr="Boot-out failed: 3: No such process\n")
    daemons.bootout(kit.system, RUNNER_LABEL)
    kit.run.answer([LAUNCHCTL, "bootout"], rc=113, stdout="Could not find service in domain\n")
    daemons.bootout(kit.system, RUNNER_LABEL)
    assert kit.run.calls == [[LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"]] * 3
    kit.run.answer([LAUNCHCTL, "bootout"], rc=5, stderr="Boot-out failed: 5: Input/output error\n")
    with refused("service_bootout_failed", RUNNER_LABEL):
        daemons.bootout(kit.system, RUNNER_LABEL)


def test_bootstrap_reloads(kit: Kit, tmp_path: Path) -> None:
    path = tmp_path / "x.plist"
    daemons.bootstrap(kit.system, path, RUNNER_LABEL)
    assert kit.run.calls == [
        [LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"],
        [LAUNCHCTL, "bootstrap", "system", str(path)],
    ]
    kit.run.answer([LAUNCHCTL, "bootstrap"], rc=5)
    with refused("service_bootstrap_failed", RUNNER_LABEL):
        daemons.bootstrap(kit.system, path, RUNNER_LABEL)


def test_install_runner_writes_and_loads(kit: Kit) -> None:
    owner = owner_of(kit.layout)
    daemons.install_runner(kit.system, owner, {"AGENT_RUNNER_HOME": "/h"})
    path = kit.layout.runner_plist
    assert path == kit.layout.root / "Library/LaunchDaemons/swiss.dfx.agent-runner.plist"
    assert plistlib.loads(path.read_bytes()) == daemons.runner_plist(kit.system, owner, {"AGENT_RUNNER_HOME": "/h"})
    assert plistlib.loads(path.read_bytes())["UserName"] == "alice"
    assert kit.run.calls == [
        [LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"],
        [LAUNCHCTL, "bootstrap", "system", str(path)],
    ]


def test_install_maintenance_loads_only_when_not_loaded(kit: Kit) -> None:
    kit.run.answer(PRINT_MAINTENANCE, rc=113)
    daemons.install_maintenance(kit.system)
    path = kit.layout.maintenance_plist
    assert plistlib.loads(path.read_bytes()) == daemons.maintenance_plist(kit.system)
    assert kit.run.calls == [
        PRINT_MAINTENANCE,
        [LAUNCHCTL, "bootout", f"system/{MAINTENANCE_LABEL}"],
        [LAUNCHCTL, "bootstrap", "system", str(path)],
    ]
    # loaded (an update runs inside it): the plist is rewritten, the daemon is not touched
    kit.run.calls.clear()
    path.unlink()
    kit.run.answer(PRINT_MAINTENANCE, rc=0)
    daemons.install_maintenance(kit.system)
    assert path.is_file()
    assert kit.run.calls == [PRINT_MAINTENANCE]


def test_remove_unloads_and_deletes(kit: Kit) -> None:
    path = kit.layout.runner_plist
    daemons.write_plist(path, {"Label": RUNNER_LABEL})
    assert daemons.remove(kit.system, RUNNER_LABEL, path) is True
    assert not path.exists()
    assert kit.run.calls == [[LAUNCHCTL, "bootout", f"system/{RUNNER_LABEL}"]]
    kit.run.calls.clear()
    assert daemons.remove(kit.system, RUNNER_LABEL, path, unload=False) is False
    assert kit.run.calls == []


def test_a_failed_unload_keeps_the_plist(kit: Kit) -> None:
    path = kit.layout.runner_plist
    daemons.write_plist(path, {"Label": RUNNER_LABEL})
    kit.run.answer([LAUNCHCTL, "bootout"], rc=5)
    with refused("service_bootout_failed", RUNNER_LABEL):
        daemons.remove(kit.system, RUNNER_LABEL, path)
    assert path.is_file()
