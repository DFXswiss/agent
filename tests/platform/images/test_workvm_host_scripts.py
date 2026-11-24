"""U-RUN-workvm host scripts: `images/workvm/host-rehearsal.sh` and `egress-check.sh` (13-work-vm.md section 6.4,
KOR-29 operations finding 1). Both need root, pf and the Cloudflare client on a test device; here they are parsed by
`/bin/sh -n` and their text is checked for the rules of the finding: never load the main ruleset or switch pf off, a
timer that survives a lost operator session, no rehearsal on a device with an installation, and the hook check on
the first `anchor` line of the main ruleset."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

WORKVM = Path(__file__).resolve().parents[3] / "images" / "workvm"
REHEARSAL = WORKVM / "host-rehearsal.sh"
EGRESS = WORKVM / "egress-check.sh"
FIRST_ANCHOR = "grep -n '^anchor ' | head -n 1"
HOOK_CASE = """*:'anchor "com.apple/*"'*)"""


@pytest.mark.parametrize("script", [REHEARSAL, EGRESS], ids=["rehearsal", "egress"])
def test_parses_is_executable_and_never_touches_the_main_ruleset(script: Path) -> None:
    assert subprocess.run(["/bin/sh", "-n", str(script)], check=False).returncode == 0
    assert os.access(script, os.X_OK)
    code = "\n".join(
        line for line in script.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"pfctl[^\n|;]*-f\s+/etc/pf\.conf", code)
    assert not re.search(r"pfctl[^\n|;]*\s-d\b", code)
    assert not re.search(r"pfctl[^\n|;]*\s-F\b", code)  # the rehearsal flushes only through `workvm uninstall`


@pytest.mark.parametrize("script", [REHEARSAL, EGRESS], ids=["rehearsal", "egress"])
def test_the_hook_is_the_first_anchor_line(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert FIRST_ANCHOR in text
    assert HOOK_CASE in text


def test_rehearsal_timer_survives_a_lost_session_and_uninstalls() -> None:
    text = REHEARSAL.read_text(encoding="utf-8")
    assert "trap '' HUP" in text
    timer = text[text.index('trap "" HUP') :]  # the timer's own shell as well
    assert timer.index('sleep "$1"') < timer.index("workvm uninstall") < timer.index("$((deadline * 60))")
    assert "nohup sh -c" in text


def test_rehearsal_refuses_a_device_with_an_installation() -> None:
    text = REHEARSAL.read_text(encoding="utf-8")
    assert "this device already has a work VM installation" in text
    refusal = text[: text.index("this device already has a work VM installation")]
    assert "/etc/sudoers.d/agent-runner-workvm" in refusal
    assert "/var/agent-workvm" in refusal


def test_rehearsal_refuses_without_arguments_and_root() -> None:
    done = subprocess.run(["/bin/sh", str(REHEARSAL)], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 2
    assert "--runner-account" in done.stderr
    if os.geteuid() == 0:
        pytest.fail("the hermetic suite never runs as root")
    args = ["--runner-account", "nobody", "--site-ip", "192.0.2.1"]
    done = subprocess.run(["/bin/sh", str(REHEARSAL), *args], capture_output=True, text=True, timeout=30, check=False)
    assert (done.returncode, done.stderr) == (2, "host-rehearsal: run as root\n")
