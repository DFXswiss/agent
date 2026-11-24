"""U-RUN-workvm accounts: slot accounts, wrapper, sudoers and the APFS volume (13-work-vm.md section 6.1).

`dscl`, `diskutil` and `visudo` are never run; the commands are data and the readers take injected fakes.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_runner.errors import RunnerError
from agent_runner.workvm import accounts


def invalid(field: str) -> tuple[str, str]:
    return ("workvm_accounts_invalid", field)


def test_slot_accounts_names_and_ports() -> None:
    assert accounts.slot_accounts("agent-", 3, 47200) == [
        (0, "agent-wvm00", 47200),
        (1, "agent-wvm01", 47201),
        (2, "agent-wvm02", 47202),
    ]
    assert accounts.slot_accounts("", 0, 1024) == []
    assert accounts.slot_accounts("", 100, 65436)[-1] == (99, "wvm99", 65535)


@pytest.mark.parametrize(
    ("prefix", "count", "base", "field"),
    [
        ("Agent", 1, 47200, "prefix"),
        ("1agent", 1, 47200, "prefix"),
        ("a" * 28, 1, 47200, "account"),
        ("a", -1, 47200, "count"),
        ("a", 101, 47200, "count"),
        ("a", 1, 1023, "ssh_port_base"),
        ("a", 2, 65535, "ssh_port_base"),
    ],
)
def test_slot_accounts_refuses(prefix: str, count: int, base: int, field: str) -> None:
    with pytest.raises(RunnerError) as caught:
        accounts.slot_accounts(prefix, count, base)
    assert (caught.value.code, caught.value.name) == invalid(field)


def test_free_uid() -> None:
    assert accounts.free_uid(set()) == 600
    assert accounts.free_uid({600, 601, 603}) == 602
    assert accounts.free_uid({700}, start=700) == 701
    with pytest.raises(RunnerError, match="start"):
        accounts.free_uid(set(), start=500)
    with pytest.raises(RunnerError, match="start"):
        accounts.free_uid({2**31}, start=2**31)


def test_group_commands() -> None:
    assert accounts.group_name("agent-") == "agent-workvm"
    assert accounts.group_commands("agent-", 650) == [
        ["/usr/bin/dscl", ".", "-create", "/Groups/agent-workvm"],
        ["/usr/bin/dscl", ".", "-create", "/Groups/agent-workvm", "PrimaryGroupID", "650"],
        ["/usr/bin/dscl", ".", "-create", "/Groups/agent-workvm", "RealName", "Agent work VM slots"],
        ["/usr/bin/dscl", ".", "-create", "/Groups/agent-workvm", "Password", "*"],
    ]
    with pytest.raises(RunnerError, match="gid"):
        accounts.group_commands("agent-", 20)
    with pytest.raises(RunnerError, match="prefix"):
        accounts.group_name("A")


def test_create_commands() -> None:
    commands = accounts.create_commands("agent-wvm00", 601, 650, "agent-workvm", "/var/agent-workvm/agent-wvm00")
    record = "/" + "Users/agent-wvm00"  # the directory-service record (dscl), not a file system path
    assert commands[:8] == [
        ["/usr/bin/dscl", ".", "-create", record],
        ["/usr/bin/dscl", ".", "-create", record, "UserShell", "/usr/bin/false"],
        ["/usr/bin/dscl", ".", "-create", record, "RealName", "Agent work VM slot agent-wvm00"],
        ["/usr/bin/dscl", ".", "-create", record, "UniqueID", "601"],
        ["/usr/bin/dscl", ".", "-create", record, "PrimaryGroupID", "650"],
        ["/usr/bin/dscl", ".", "-create", record, "NFSHomeDirectory", "/var/agent-workvm/agent-wvm00"],
        ["/usr/bin/dscl", ".", "-create", record, "IsHidden", "1"],
        ["/usr/bin/dscl", ".", "-create", record, "Password", "*"],
    ]
    home = "/var/agent-workvm/agent-wvm00"
    assert commands[8] == ["/usr/bin/install", "-d", "-o", "agent-wvm00", "-g", "agent-workvm", "-m", "0700", home]
    owner = ["-o", "agent-wvm00", "-g", "agent-workvm", "-m", "0700"]
    # Lima writes _config/networks.yaml at the first start: the whole Lima home belongs to the account (HOT-18)
    assert commands[9] == ["/usr/bin/install", "-d", *owner, f"{home}/lima"]
    assert commands[10] == ["/usr/bin/install", "-d", *owner, f"{home}/lima/_config"]
    assert commands[-1] == ["/bin/chmod", "0600", f"{home}/lima/_config/user"]
    assert not any("root" in argv for argv in commands)


@pytest.fixture
def chowned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int]]:
    """`os.fchown` calls (descriptor, uid, gid): a test account cannot give a directory to another account."""
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((fd, uid, gid)))
    return calls


def lima_home(root: Path, config: bool = True) -> Path:
    home = root / "wvm00"
    (home / "lima").mkdir(parents=True)
    if config:
        (home / "lima" / "_config").mkdir()
        (home / "lima" / "_config").chmod(0o755)
    return home


def test_give_lima_config_gives_a_root_owned_config_to_the_account(
    tmp_path: Path, chowned: list[tuple[int, int, int]]
) -> None:
    home = lima_home(tmp_path)
    assert accounts.give_lima_config(str(home), 602, 601) == "present"
    assert [(uid, gid) for _, uid, gid in chowned] == [(602, 601)]
    assert stat.S_IMODE((home / "lima" / "_config").stat().st_mode) == 0o700


def test_give_lima_config_creates_a_missing_config(tmp_path: Path, chowned: list[tuple[int, int, int]]) -> None:
    home = lima_home(tmp_path, config=False)
    assert accounts.give_lima_config(str(home), 602, 601) == "created"
    assert stat.S_IMODE((home / "lima" / "_config").stat().st_mode) == 0o700
    assert len(chowned) == 1


@pytest.mark.parametrize("planted", ["lima", "_config"])
def test_give_lima_config_follows_no_symbolic_link(
    tmp_path: Path, chowned: list[tuple[int, int, int]], planted: str
) -> None:
    other = lima_home(tmp_path / "other")  # another slot's home
    home = tmp_path / "wvm00"
    if planted == "lima":
        home.mkdir()
        (home / "lima").symlink_to(other / "lima")
    else:
        (home / "lima").mkdir(parents=True)
        (home / "lima" / "_config").symlink_to(other / "lima" / "_config")
    with pytest.raises(RunnerError, match="lima_config") as caught:
        accounts.give_lima_config(str(home), 602, 601)
    assert caught.value.code == "workvm_install_failed"
    assert chowned == []
    assert stat.S_IMODE((other / "lima" / "_config").stat().st_mode) == 0o755


def test_give_lima_config_refuses_a_missing_lima_home_and_bad_values(
    tmp_path: Path, chowned: list[tuple[int, int, int]]
) -> None:
    (tmp_path / "wvm00").mkdir()
    with pytest.raises(RunnerError, match="lima_config"):
        accounts.give_lima_config(str(tmp_path / "wvm00"), 602, 601)
    home = lima_home(tmp_path / "x")
    with pytest.raises(RunnerError, match="uid"):
        accounts.give_lima_config(str(home), 0, 601)
    with pytest.raises(RunnerError, match="home"):
        accounts.give_lima_config("relative/home", 602, 601)
    assert chowned == []


@pytest.mark.parametrize(
    ("args", "field"),
    [
        (("Bad", 601, 650, "g", "/var/x"), "account"),
        (("a", 601, 650, "G", "/var/x"), "account"),
        (("a", 0, 650, "g", "/var/x"), "uid"),
        (("a", 601, 0, "g", "/var/x"), "gid"),
        (("a", 601, 650, "g", "var/x"), "home"),
        (("a", 601, 650, "g", "/var/../etc"), "home"),
        (("a", 601, 650, "g", "/var//x"), "home"),
        (("a", 601, 650, "g", "/var/x y"), "home"),
    ],
)
def test_create_commands_refuses(args: tuple[str, int, int, str, str], field: str) -> None:
    with pytest.raises(RunnerError) as caught:
        accounts.create_commands(*args)
    assert (caught.value.code, caught.value.name) == invalid(field)


def run_wrapper(tmp_path: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "wrapper"
    path.write_text(script)
    return subprocess.run(["/bin/sh", str(path), *args], capture_output=True, text=True, check=False, timeout=30)


def test_wrapper_script_runs_limactl_with_a_fixed_environment(tmp_path: Path) -> None:
    me = subprocess.run(["/usr/bin/id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    if not accounts._ACCOUNT_RE.fullmatch(me):
        pytest.fail("the test account name is not a valid slot account name")
    (tmp_path / me / "lima").mkdir(parents=True)
    fake = tmp_path / "bin" / "limactl"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho "HOME=$HOME LIMA_HOME=$LIMA_HOME PATH=$PATH SECRET=${SECRET-} args=$*"\n')
    fake.chmod(0o755)
    script = accounts.wrapper_script(["other", me], str(tmp_path), str(fake))
    result = run_wrapper(tmp_path, script, "list", "--json")
    home = tmp_path / me
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"HOME={home} LIMA_HOME={home}/lima PATH={fake.parent}:/usr/bin:/bin SECRET= args=list --json"
    )
    refused = run_wrapper(tmp_path, accounts.wrapper_script(["other"], str(tmp_path), str(fake)), "list")
    assert refused.returncode == 126
    assert "refused" in refused.stderr


def test_wrapper_script_defaults_and_refusals() -> None:
    text = accounts.wrapper_script(["agent-wvm00", "agent-wvm01"])
    assert text.startswith("#!/bin/sh\n")
    assert "  agent-wvm00|agent-wvm01) ;;" in text
    assert "home='/var/agent-workvm'/\"$user\"" in text
    assert "PATH='/opt/homebrew/bin:/usr/bin:/bin' '/opt/homebrew/bin/limactl' \"$@\"" in text
    assert "PATH='/:/usr/bin:/bin'" in accounts.wrapper_script(["a"], limactl="/limactl")
    with pytest.raises(RunnerError, match="accounts"):
        accounts.wrapper_script([])
    with pytest.raises(RunnerError, match="account"):
        accounts.wrapper_script(["a;rm"])
    with pytest.raises(RunnerError, match="home_root"):
        accounts.wrapper_script(["a"], home_root="/var/$(x)")
    with pytest.raises(RunnerError, match="limactl"):
        accounts.wrapper_script(["a"], limactl="limactl")


def test_sudoers() -> None:
    assert accounts.sudoers("agent", ["agent-wvm00", "agent-wvm01"]) == (
        "# agent-runner work VM slots, generated by agent-runner workvm install (spec 11 section 6.1).\n"
        "Defaults!/usr/local/libexec/agent-runner-workvm-limactl env_reset\n"
        "Runas_Alias AGENT_WORKVM = agent-wvm00, agent-wvm01\n"
        "agent ALL = (AGENT_WORKVM) NOPASSWD: /usr/local/libexec/agent-runner-workvm-limactl\n"
        "agent ALL = (root) NOPASSWD: /sbin/pfctl -s info, /sbin/pfctl -a com.apple/000.agent.workvm -sr,"
        " /sbin/pfctl -sr, /sbin/pfctl -s Interfaces -v,"
        " /sbin/pfctl -a com.apple/000.agent.workvm -t workvm_blocked -T show, /sbin/pfctl -a com.apple -sA\n"
    )
    with pytest.raises(RunnerError, match="account"):
        accounts.sudoers("ALL", ["a"])
    with pytest.raises(RunnerError, match="accounts"):
        accounts.sudoers("agent", [])
    with pytest.raises(RunnerError, match="wrapper"):
        accounts.sudoers("agent", ["a"], wrapper="/usr/bin/*")


def test_delete_commands() -> None:
    assert accounts.delete_command("agent-wvm00") == ["/usr/bin/dscl", ".", "-delete", "/" + "Users/agent-wvm00"]
    assert accounts.delete_group_command("agent-workvm") == ["/usr/bin/dscl", ".", "-delete", "/Groups/agent-workvm"]
    assert accounts.delete_volume_command() == ["/usr/sbin/diskutil", "apfs", "deleteVolume", "/var/agent-workvm"]
    with pytest.raises(RunnerError, match="account"):
        accounts.delete_command("a;rm")
    with pytest.raises(RunnerError, match="account"):
        accounts.delete_group_command("ALL")
    with pytest.raises(RunnerError, match="mountpoint"):
        accounts.delete_volume_command("/var/../x")


def test_sudoers_check_feeds_visudo_on_stdin() -> None:
    calls: list[tuple[list[str], str, float]] = []

    def run(argv: Sequence[str], text: str, timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), text, timeout))
        return subprocess.CompletedProcess(list(argv), 0 if "NOPASSWD" in text else 1, "", "")

    assert accounts.sudoers_check(run, accounts.sudoers("agent", ["a"]))
    assert not accounts.sudoers_check(run, "garbage")
    assert calls[0][0] == ["/usr/sbin/visudo", "-c", "-f", "-"]
    assert calls[0][2] == accounts.COMMAND_TIMEOUT


def test_rendered_sudoers_passes_the_real_visudo_syntax_check() -> None:
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"

    def run(argv: Sequence[str], text: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [visudo, *argv[1:]], check=False, input=text, capture_output=True, text=True, timeout=timeout
        )

    assert accounts.sudoers_check(run, accounts.sudoers("agent", ["agent-wvm00", "agent-wvm01"]))
    assert not accounts.sudoers_check(run, "agent ALL = (\n")


def diskutil(code: int, out: str) -> accounts.Run:
    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert list(argv) == ["/usr/sbin/diskutil", "info", "-plist", "/"]
        return subprocess.CompletedProcess(list(argv), code, out, "")

    return run


def test_apfs_container() -> None:
    out = plistlib.dumps({"APFSContainerReference": "disk3", "ParentWholeDisk": "disk3"}).decode()
    assert accounts.apfs_container(diskutil(0, out)) == "disk3"


@pytest.mark.parametrize(
    ("code", "out"),
    [
        (1, ""),
        (0, "not a plist"),
        (0, "<?xml version='1.0'?><plist><dict><key>"),
        (0, plistlib.dumps(["disk3"]).decode()),
        (0, plistlib.dumps({"APFSContainerReference": "disk3s1; rm"}).decode()),
        (0, plistlib.dumps({}).decode()),
    ],
)
def test_apfs_container_refuses(code: int, out: str) -> None:
    with pytest.raises(RunnerError) as caught:
        accounts.apfs_container(diskutil(code, out))
    assert (caught.value.code, caught.value.name) == ("workvm_volume_container", "diskutil")


def test_apfs_volume_commands() -> None:
    assert accounts.apfs_volume_commands("disk3", 200) == [
        ["/bin/mkdir", "-p", "/var/agent-workvm"],
        [
            "/usr/sbin/diskutil",
            "apfs",
            "addVolume",
            "disk3",
            "APFS",
            "AgentWorkVM",
            "-quota",
            str(200 * 2**30),
            "-mountpoint",
            "/var/agent-workvm",
        ],
        ["/usr/sbin/chown", "root:wheel", "/var/agent-workvm"],
        ["/bin/chmod", "0755", "/var/agent-workvm"],
    ]


@pytest.mark.parametrize(
    ("container", "quota", "name", "mount", "field"),
    [
        ("disk3s1", 1, "AgentWorkVM", "/var/agent-workvm", "container"),
        ("disk3", 0, "AgentWorkVM", "/var/agent-workvm", "quota_gib"),
        ("disk3", 1_000_001, "AgentWorkVM", "/var/agent-workvm", "quota_gib"),
        ("disk3", 1, "Agent Work", "/var/agent-workvm", "volume_name"),
        ("disk3", 1, "AgentWorkVM", "relative", "mountpoint"),
    ],
)
def test_apfs_volume_commands_refuses(container: str, quota: int, name: str, mount: str, field: str) -> None:
    with pytest.raises(RunnerError) as caught:
        accounts.apfs_volume_commands(container, quota, name, mount)
    assert (caught.value.code, caught.value.name) == invalid(field)
