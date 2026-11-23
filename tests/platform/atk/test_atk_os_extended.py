"""Extended OS probe of the final attack gate (ATS-03; 05-runner.md sections 4.3-4.5, ADR-008, ADR-045).

Regression of the layer-2 gaps found by ATS-03, run by ``scripts/run-atk.sh`` while attack sessions run: in a unit
identical to a session's, code that got past Claude Code finds no fileless exec (``memfd_exec``), no reachable unix
socket of the system (``unix_sockets``: the resolver's varlink API was a DNS channel past the egress rules), a Python
started by the ELF interpreter stays inside the same restrictions (``ld_so_python_contained``, decisions SBX-02
no. 25), no executable mapping of writable files, no PID 1 and no ptrace; outside the unit the guest API process sees
no transient unit file (A-19) and the boot-time internet jobs of the base image are masked (A-20).
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_doubles.atk import catalog
from tests.platform.atk.fakes import skip_unselected

pytestmark = pytest.mark.atk
REQUIRED = (
    "memfd_exec",
    "unix_sockets",
    "unix_sockets_root_list",
    "ld_so_python_contained",
    "mmap_exec_workspace",
    "mmap_exec_tmp",
    "proc_1_environ",
    "ptrace",
)


@pytest.mark.parametrize("mode", catalog.MODES)
def test_extended_os_probe(atk_results: dict[str, Any], mode: str) -> None:
    skip_unselected(None, mode)
    extended = atk_results["os_probe"][mode]["extended"]
    assert "error" not in extended, extended
    checks = {check["check"]: check for check in extended["unit"]["checks"]}
    failed = {name: check["outcome"] for name, check in checks.items() if not check["ok"]}
    assert not failed, (failed, extended["unit"].get("stderr"))
    assert set(REQUIRED) <= set(checks)
    root = {check["check"]: check for check in extended["root_checks"]}
    assert root["api_transient_hidden"]["outcome"] == "hidden"
    assert root["boot_jobs_masked"]["outcome"] == "masked"
    assert extended["ok"] is True
