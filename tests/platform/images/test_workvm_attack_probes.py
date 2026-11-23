"""U-RUN-workvm attack probes: `images/workvm/attack-probes.sh` (docs/platform/atk/WVM.md, 13-work-vm.md section 11).

The probes themselves run as root inside a work VM on an execution device; here the script is parsed by `/bin/sh -n`,
its refusals run for real, it names every case, keeps the own slot's SSH port out of the host probes, and its
credential search (made hermetic by `WVM_PROBE_ROOT`) reports counts and paths but never the matched text.
Token literals are assembled at run time (repository hygiene rule).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "images" / "workvm" / "attack-probes.sh"
CASES = tuple(f"WVM-{n:02d}" for n in range(1, 11))
FAKE_TOKEN = "gh" + "p_" + "Q7" * 18
FAKE_ENV_TOKEN = "sk-" + "ant-" + "Z9" * 12


def run(*args: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "WVM_PROBE_ROOT"}
    if root is not None:
        env["WVM_PROBE_ROOT"] = str(root)
    return subprocess.run(
        ["/bin/sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=60, check=False, env=env
    )


def test_script_parses_and_is_executable() -> None:
    assert subprocess.run(["/bin/sh", "-n", str(SCRIPT)], check=False).returncode == 0
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--slot-ports",),
        ("--slot-ports", ""),
        ("--slot-ports", "47200,abc"),
        ("--slot-ports", "70000"),
        ("--slot-ports", "47200", "--peer", "not an address"),
        ("--slot-ports", "47200", "--site-ip"),
        ("--slot-ports", "47200", "--fill-gib", "x"),
        ("--slot-ports", "47200", "--cases", "WVM-11"),
        ("--slot-ports", "47200", "--cases", ""),
        ("--slot-ports", "47200", "--bogus"),
    ],
)
def test_usage_errors_exit_2(args: tuple[str, ...]) -> None:
    done = run(*args)
    assert done.returncode == 2
    assert "--slot-ports" in done.stderr
    assert done.stdout == ""


def test_refuses_to_run_without_root() -> None:
    if os.geteuid() == 0:
        pytest.fail("the hermetic suite never runs as root")
    done = run("--slot-ports", "47200")
    assert (done.returncode, done.stdout, done.stderr) == (2, "", "attack-probes: run as root\n")


def test_probe_root_must_be_an_absolute_directory(tmp_path: Path) -> None:
    for root in (Path("relative"), tmp_path / "missing"):
        done = run("--slot-ports", "47200", "--cases", "WVM-06", root=root)
        assert done.returncode == 2
        assert done.stdout == ""


def test_lists_every_case() -> None:
    done = run("--list-cases")
    assert done.returncode == 0
    listed = [line.split()[0] for line in done.stdout.splitlines()]
    assert tuple(listed) == CASES
    text = SCRIPT.read_text(encoding="utf-8")
    for case in CASES:
        assert f"report {case} " in text


def test_host_ports_exclude_the_own_slot_and_include_the_others() -> None:
    done = run("--slot-ports", "47203,47200,47201,47203", "--print-ports")
    assert done.returncode == 0
    ports = done.stdout.split()
    assert "47203" not in ports
    assert {"47200", "47201", "5432", "7700", "8000"} <= set(ports)
    assert len(ports) == len(set(ports))


def test_own_port_is_excluded_even_when_it_is_a_fixed_port() -> None:
    ports = run("--slot-ports", "5432", "--print-ports").stdout.split()
    assert "5432" not in ports
    assert "7700" in ports


def test_credential_search_reports_counts_and_paths_never_the_secret(tmp_path: Path) -> None:
    (tmp_path / "root" / ".config").mkdir(parents=True)
    (tmp_path / "root" / ".config" / "hub.txt").write_text(f"token={FAKE_TOKEN}\nagain {FAKE_TOKEN}\n")
    (tmp_path / "proc" / "1").mkdir(parents=True)
    (tmp_path / "proc" / "1" / "environ").write_bytes(f"HOME=/root\0API_KEY={FAKE_ENV_TOKEN}\0".encode())
    (tmp_path / "home" / "lima" / ".ssh").mkdir(parents=True)
    (tmp_path / "home" / "lima" / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAAC3Nz test\n")
    (tmp_path / "mnt" / "lima-cidata").mkdir(parents=True)
    (tmp_path / "mnt" / "lima-cidata" / "lima-guestagent").write_bytes(b"\x7fELF")
    (tmp_path / "mnt" / "lima-cidata" / "param.env").write_text("# none\nNAME=value\n")
    done = run("--slot-ports", "47200", "--cases", "WVM-06", root=tmp_path)
    assert done.returncode == 1
    assert FAKE_TOKEN not in done.stdout + done.stderr
    assert FAKE_ENV_TOKEN not in done.stdout + done.stderr
    assert "value" not in done.stdout
    lines = done.stdout.splitlines()
    assert "case=WVM-06 item=lima-guestagent status=expected path=/mnt/lima-cidata/lima-guestagent" in lines
    assert "case=WVM-06 item=lima-ssh-key status=expected path=/home/lima/.ssh/authorized_keys" in lines
    assert "case=WVM-06 item=environ status=found path=/proc/1/environ matches=1" in lines
    assert "case=WVM-06 item=root-home status=found path=/root/.config/hub.txt matches=2" in lines
    assert "case=WVM-06 item=lima-param status=found path=/mnt/lima-cidata/param.env matches=1" in lines
    assert lines[-1] == "case=WVM-06 name=credentials result=fail detail=files=3 matches=4 cidata-iso=not-mounted"


def test_credential_search_passes_with_only_the_expected_pieces(tmp_path: Path) -> None:
    (tmp_path / "home" / "lima" / ".ssh").mkdir(parents=True)
    (tmp_path / "home" / "lima" / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAAC3Nz test\n")
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "environment").write_text('PATH="/usr/local/bin:/usr/bin"\n')
    (tmp_path / "mnt" / "lima-cidata").mkdir(parents=True)
    (tmp_path / "mnt" / "lima-cidata" / "param.env").write_text("")
    done = run("--slot-ports", "47200", "--cases", "WVM-06,WVM-08", root=tmp_path)
    assert done.returncode == 0
    assert done.stdout.splitlines() == [
        "case=WVM-06 item=lima-ssh-key status=expected path=/home/lima/.ssh/authorized_keys",
        "case=WVM-06 name=credentials result=pass detail=files=0 matches=0 cidata-iso=not-mounted",
        "case=WVM-08 name=request-abuse result=skip detail=host-side, see runner tests",
    ]
