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
REBOOT = WORKVM / "after-reboot-check.sh"
FIRST_ANCHOR = "grep -n '^anchor ' | head -n 1"
HOOK_CASE = """*:'anchor "com.apple/*"'*)"""


@pytest.mark.parametrize("script", [REHEARSAL, EGRESS, REBOOT], ids=["rehearsal", "egress", "reboot"])
def test_parses_is_executable_and_never_touches_the_main_ruleset(script: Path) -> None:
    assert subprocess.run(["/bin/sh", "-n", str(script)], check=False).returncode == 0
    assert os.access(script, os.X_OK)
    code = "\n".join(
        line for line in script.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"pfctl[^\n|;]*-f\s+/etc/pf\.conf", code)
    assert not re.search(r"pfctl[^\n|;]*\s-d\b", code)
    # the rehearsal flushes only through `workvm uninstall`, and its own scratch probe table (a child without rules)
    flushes = re.findall(r"pfctl[^\n|;]*\s-F\b[^\n]*", code)
    assert all("com.apple/zzz.agent-workvm-probe" in line for line in flushes)


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


def test_egress_check_covers_tcp_udp_and_icmp_to_the_site_network() -> None:
    text = EGRESS.read_text(encoding="utf-8")
    for name in ("lan_udp", "lan_icmp_slot_socket", "vm_lan_icmp", "vm_lan_tcp", "vm_lan_udp"):
        assert f"report {name} " in text
    assert "192.0.2.1" in text  # a documentation address that answers marks faked echo replies


def test_after_reboot_check_is_read_only_and_refuses_without_root() -> None:
    text = REBOOT.read_text(encoding="utf-8")
    for name in ("volume_mounted", "volume_name", "watcher_state", "anchor_first", "hook"):
        assert f"report {name} " in text
    assert "after-reboot-check.sh" in REHEARSAL.read_text(encoding="utf-8")
    done = subprocess.run(["/bin/sh", str(REBOOT), "--accounts", "x"], capture_output=True, text=True, check=False)
    assert (done.returncode, done.stderr) == (2, "after-reboot-check: --accounts needs a number\n")
    done = subprocess.run(["/bin/sh", str(REBOOT), "--bogus"], capture_output=True, text=True, check=False)
    assert done.returncode == 2
    if os.geteuid() == 0:
        pytest.fail("the hermetic suite never runs as root")
    done = subprocess.run(["/bin/sh", str(REBOOT)], capture_output=True, text=True, check=False)
    assert (done.returncode, done.stderr) == (2, "after-reboot-check: run as root\n")


def test_rehearsal_sets_the_runner_home_private_and_saves_pf_state_from_stdout_only() -> None:
    text = REHEARSAL.read_text(encoding="utf-8")
    assert 'mkdir -p "$home" && chmod 0700 "$home"' in text  # the runner refuses a readable home, whatever the umask
    assert "umask" not in text  # a global umask would also narrow what `install` creates
    saved = [line for line in text.splitlines() if line.startswith("pfctl ") and '>"$log/before.' in line]
    assert saved
    assert all("2>/dev/null" in line and "2>&1" not in line for line in saved)  # as the comparisons (ALTQ notices)
    assert 'pfctl -sA 2>/dev/null | cmp -s - "$log/before.anchors"' in text


def test_rehearsal_probes_continuously_and_saves_the_state_of_every_hit() -> None:
    text = REHEARSAL.read_text(encoding="utf-8")
    loop = text[text.index('sudo -n -u "$account" /bin/sh -c \'') :]
    assert "sleep 0.1" in loop  # about every 100 ms
    for target in ("hit loopback", "hit site_address", "hit internet_disconnected"):
        assert target in loop
    snapshot = text[text.index("snapshot() {") : text.index('if [ -n "$port" ]; then')]
    for saved in (
        "anchor.txt",
        "table.txt",
        "children.txt",
        "main.txt",
        "client.txt",
        "states.txt",
        "info.txt",
        "watcher.txt",
    ):
        assert saved in snapshot
    assert text.index("stop_prober\n") < text.index('"$runner" workvm uninstall >"$log/uninstall.txt"')
    assert "pf-table-probe.txt" in text  # how pf takes `::` into a table, read back from pf itself
