"""U-RUN-workvm egress check: the operator's script `images/workvm/egress-check.sh` (13-work-vm.md section 6.3).

The checks themselves need root, the Cloudflare client and slot accounts on an execution device; here the script is
parsed by `/bin/sh -n` and its refusals run for real (usage, not root). It names the read commands of the anchor and
never prints anything but addresses and codes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_runner.workvm import accounts

SCRIPT = Path(__file__).resolve().parents[3] / "images" / "workvm" / "egress-check.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, check=False)


def test_script_parses_and_is_executable() -> None:
    assert subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=False).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize("args", [(), ("--account", "a"), ("--site-ip", "192.0.2.1"), ("--bogus",)])
def test_usage_without_the_required_arguments(args: tuple[str, ...]) -> None:
    done = run(*args)
    assert done.returncode == 2
    assert "--account" in done.stderr
    assert done.stdout == ""


def test_refuses_to_run_without_root() -> None:
    if os.geteuid() == 0:
        pytest.fail("the hermetic suite never runs as root")
    done = run("--account", "nobody", "--site-ip", "192.0.2.1")
    assert (done.returncode, done.stderr) == (2, "egress-check: run as root\n")


def test_script_uses_the_installed_wrapper_and_the_checks_of_the_issue() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert f"wrapper={accounts.WRAPPER_PATH}\n" in text
    for check in ("slot_egress", "slot_ipify", "slot_ipv6", "slot_http3", "port25", "loopback", "disconnected"):
        assert f" {check} " in text or f"report {check}" in text or f"egress_ok {check}" in text
