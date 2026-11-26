"""Checks of a Mac before it becomes an execution device (ADR-074 section 3).

One result per check, in fixed order, each with a closed code when it fails: macOS on Apple silicon at a supported
version, virtualization available to processes, enough memory and free space. Nothing is changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_runner.install.system import System

MIN_MACOS = (14, 0)
MIN_MEMORY_GIB = 16
MIN_FREE_GIB = 50
GIB = 1024**3
_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.\d+)?$")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    value: str
    code: str = ""

    def fields(self) -> dict[str, str | bool]:
        fields: dict[str, str | bool] = {"name": self.name, "ok": self.ok, "value": self.value}
        if self.code:
            fields["code"] = self.code
        return fields


def _sysctl(system: System, name: str) -> str | None:
    done = system.run(["/usr/sbin/sysctl", "-n", name])
    return done.stdout.strip() if done.returncode == 0 else None


def macos_version(system: System) -> tuple[int, int] | None:
    done = system.run(["/usr/bin/sw_vers", "-productVersion"])
    match = _VERSION_RE.fullmatch(done.stdout.strip()) if done.returncode == 0 else None
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def run_checks(system: System) -> list[Check]:
    if system.platform != "darwin":
        return [Check("os", False, system.platform, "unsupported_os")]
    checks = [Check("os", True, "darwin")]
    version = macos_version(system)
    shown = "unknown" if version is None else f"{version[0]}.{version[1]}"
    checks.append(Check("macos", version is not None and version >= MIN_MACOS, shown, "macos_too_old"))
    machine = system.machine()
    checks.append(Check("arch", machine == "arm64", machine, "not_apple_silicon"))
    hv = _sysctl(system, "kern.hv_support")
    checks.append(Check("virtualization", hv == "1", hv or "unknown", "virtualization_unavailable"))
    memory = _sysctl(system, "hw.memsize")
    gib = int(memory) // GIB if memory is not None and memory.isdigit() else 0
    checks.append(Check("memory", gib >= MIN_MEMORY_GIB, f"{gib}GiB", "memory_too_small"))
    free = system.disk_free(system.layout.root) // GIB
    checks.append(Check("disk", free >= MIN_FREE_GIB, f"{free}GiB", "disk_too_full"))
    return [check if not check.ok else Check(check.name, True, check.value) for check in checks]


def failures(checks: list[Check]) -> list[str]:
    return [check.code for check in checks if not check.ok]
